from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class SignupIn(BaseModel):
    email: str
    password: str = Field(min_length=8)


class LoginIn(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: int
    email: str

    model_config = {"from_attributes": True}


class SessionOut(BaseModel):
    user: UserOut
    csrf: str


class VideoIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str
    size_bytes: int


class JobOut(BaseModel):
    id: int
    filename: str
    content_type: str
    size_bytes: int
    status: str
    error: Optional[str]
    playlist_url: Optional[str]

    model_config = {"from_attributes": True}


class VideoCreated(BaseModel):
    job: JobOut
    upload_url: str
