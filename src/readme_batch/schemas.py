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
