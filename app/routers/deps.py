from fastapi import Depends, Header, HTTPException

from .. import security


def require_admin(authorization: str = Header(default="")):
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token or not security.verify_admin_token(token):
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return True
