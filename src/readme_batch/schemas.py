from typing import Literal

from pydantic import BaseModel, Field


class SessionRequest(BaseModel):
    token: str = Field(min_length=1)
    remember: bool = False


class DraftRequest(BaseModel):
    selected: list[str] = Field(default_factory=list)
    old: str = ""
    new: str = ""
    commit_message: str = ""


class RetryRequest(BaseModel):
    full_name: str = Field(min_length=1)


class SettingsOperationRequest(BaseModel):
    selected: list[str] = Field(default_factory=list)
    names: dict[str, str] = Field(default_factory=dict)
    visibility: Literal["keep", "public", "private"] = "keep"


class LabelsOperationRequest(BaseModel):
    selected: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class UploadFileRequest(BaseModel):
    name: str
    content_base64: str


class UploadOperationRequest(BaseModel):
    selected: list[str] = Field(default_factory=list)
    directory: str = ""
    overwrite: bool = False
    message: str = "chore: 批量上传文件"
    files: list[UploadFileRequest] = Field(default_factory=list)
