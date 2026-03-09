import os
import json
import secrets
import string
import asyncio
import redis
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import jwt

from db import Base, engine, SessionLocal, get_db, User, Link

REDIS_URL = os.getenv("REDIS_URL", "").strip()
SECRET_KEY = os.getenv("SECRET_KEY", "secret")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
redis_client = redis.from_url(REDIS_URL) if REDIS_URL else None


class UserCreate(BaseModel):
    username: str
    password: str


class LinkShorten(BaseModel):
    original_url: str
    custom_alias: str = None
    expires_at: datetime = None


class LinkUpdate(BaseModel):
    original_url: str


def hash_password(password):
    p = str(password).encode("utf-8")[:72].decode("utf-8", errors="replace")
    return pwd_context.hash(p)


def check_password(plain, hashed):
    return pwd_context.verify(plain, hashed)


def make_token(username):
    data = {"sub": username, "exp": datetime.utcnow() + timedelta(days=1)}
    return jwt.encode(data, SECRET_KEY, algorithm="HS256")


def read_token(token):
    try:
        d = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return d.get("sub")
    except:
        return None


def get_short_code():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(6))


def cache_get(key):
    if not redis_client:
        return None
    val = redis_client.get(key)
    return json.loads(val) if val else None


def cache_set(key, val, ttl=300):
    if redis_client:
        redis_client.setex(key, ttl, json.dumps(val, default=str))


def cache_del(key):
    if redis_client:
        redis_client.delete(key)


async def delete_expired():
    while True:
        await asyncio.sleep(60)
        db = SessionLocal()
        try:
            for link in db.query(Link).filter(Link.expires_at <= datetime.utcnow()).all():
                db.delete(link)
            db.commit()
        finally:
            db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    t = asyncio.create_task(delete_expired())
    yield
    t.cancel()


app = FastAPI(lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


def get_user(bearer_data: HTTPAuthorizationCredentials = Depends(bearer)):
    if bearer_data is None:
        return None
    return read_token(bearer_data.credentials)


def require_user(bearer_data: HTTPAuthorizationCredentials = Depends(HTTPBearer(auto_error=True))):
    u = read_token(bearer_data.credentials)
    if u is None:
        raise HTTPException(status_code=401, detail="bad token")
    return u


@app.get("/")
def root():
    return {"message": "ok"}


@app.post("/auth/register")
def register(data: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(status_code=400, detail="username exists")
    u = User(username=data.username, hashed_password=hash_password(data.password))
    db.add(u)
    db.commit()
    db.refresh(u)
    return {"id": u.id, "username": u.username}


@app.post("/auth/login")
def login(data: UserCreate, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.username == data.username).first()
    if not u or not check_password(data.password, u.hashed_password):
        raise HTTPException(status_code=401, detail="wrong password")
    return {"access_token": make_token(u.username), "token_type": "bearer"}


@app.post("/links/shorten")
def shorten(data: LinkShorten, db: Session = Depends(get_db), username=Depends(get_user)):
    user_id = None
    if username:
        u = db.query(User).filter(User.username == username).first()
        if u:
            user_id = u.id
    code = data.custom_alias if data.custom_alias else get_short_code()
    for _ in range(5):
        if not db.query(Link).filter(Link.short_code == code).first():
            break
        if data.custom_alias:
            raise HTTPException(status_code=400, detail="alias exists")
        code = get_short_code()
    else:
        raise HTTPException(status_code=500, detail="try again")
    link = Link(short_code=code, original_url=data.original_url, custom_alias=data.custom_alias,
                user_id=user_id, expires_at=data.expires_at)
    db.add(link)
    db.commit()
    db.refresh(link)
    return {"short_code": link.short_code, "original_url": link.original_url,
            "created_at": link.created_at, "expires_at": link.expires_at}


@app.get("/links/search")
def search(original_url: str, db: Session = Depends(get_db)):
    links = db.query(Link).filter(Link.original_url == original_url).all()
    return [{"short_code": l.short_code, "original_url": l.original_url} for l in links]


@app.get("/links/{code}")
def redirect(code: str, db: Session = Depends(get_db)):
    cached = cache_get("link:" + code)
    if cached:
        link = db.query(Link).filter(Link.short_code == code).first()
        if link:
            link.click_count += 1
            link.last_used_at = datetime.utcnow()
            db.commit()
        cache_del("stats:" + code)
        return RedirectResponse(url=cached["url"], status_code=302)
    link = db.query(Link).filter(Link.short_code == code).first()
    if not link:
        raise HTTPException(status_code=404, detail="not found")
    if link.expires_at and link.expires_at <= datetime.utcnow():
        db.delete(link)
        db.commit()
        raise HTTPException(status_code=404, detail="expired")
    cache_set("link:" + code, {"url": link.original_url})
    link.click_count += 1
    link.last_used_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url=link.original_url, status_code=302)


@app.get("/links/{code}/stats")
def stats(code: str, db: Session = Depends(get_db)):
    cached = cache_get("stats:" + code)
    if cached:
        return cached
    link = db.query(Link).filter(Link.short_code == code).first()
    if not link:
        raise HTTPException(status_code=404, detail="not found")
    if link.expires_at and link.expires_at <= datetime.utcnow():
        raise HTTPException(status_code=404, detail="expired")
    res = {"original_url": link.original_url, "created_at": link.created_at,
           "click_count": link.click_count, "last_used_at": link.last_used_at, "expires_at": link.expires_at}
    cache_set("stats:" + code, res)
    return res


@app.delete("/links/{code}")
def delete_link(code: str, db: Session = Depends(get_db), username=Depends(require_user)):
    link = db.query(Link).filter(Link.short_code == code).first()
    if not link:
        raise HTTPException(status_code=404, detail="not found")
    u = db.query(User).filter(User.username == username).first()
    if not u or link.user_id != u.id:
        raise HTTPException(status_code=403, detail="not your link")
    db.delete(link)
    db.commit()
    cache_del("link:" + code)
    cache_del("stats:" + code)
    return {"status": "deleted"}


@app.put("/links/{code}")
def update_link(code: str, data: LinkUpdate, db: Session = Depends(get_db), username=Depends(require_user)):
    link = db.query(Link).filter(Link.short_code == code).first()
    if not link:
        raise HTTPException(status_code=404, detail="not found")
    u = db.query(User).filter(User.username == username).first()
    if not u or link.user_id != u.id:
        raise HTTPException(status_code=403, detail="not your link")
    link.original_url = data.original_url
    db.commit()
    db.refresh(link)
    cache_del("link:" + code)
    cache_del("stats:" + code)
    return {"short_code": link.short_code, "original_url": link.original_url,
            "created_at": link.created_at, "expires_at": link.expires_at}
