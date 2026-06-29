from pydantic import BaseModel


class RunnerStatusRead(BaseModel):
    mode: str
    selected_mode: str
    docker_available: bool
    available: bool
    detail: str
    image: str | None = None
    image_status: str | None = None
    image_detail: str | None = None
    image_build_log: str | None = None
    image_error: str | None = None
