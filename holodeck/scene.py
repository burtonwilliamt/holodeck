import dataclasses


@dataclasses.dataclass
class Scene:
    name: str
    creator: int
    audio_url: str | None
    audio_path: str
    start_time_millis: int
    runtime_millis: int
    image_url: str
