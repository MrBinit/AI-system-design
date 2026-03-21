from pydantic import BaseModel, ConfigDict, Field


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    roles: list[str] = Field(default_factory=list)


class PasswordLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class PasswordLoginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str = Field(min_length=16)
    token_type: str = Field(default="bearer", min_length=3, max_length=16)
    user_id: str = Field(min_length=1, max_length=128)
    roles: list[str] = Field(default_factory=list)
    expires_in_seconds: int = Field(ge=1, le=86400)
