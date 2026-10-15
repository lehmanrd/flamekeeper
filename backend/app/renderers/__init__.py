from app.renderers.base import ConfigRenderer
from app.renderers.env import EnvRenderer
from app.renderers.json_file import JsonFileRenderer

# See docs/game-server-behavior.md: the image leaves the JSON alone when no
# SERVER_GS_*/SERVER_ROLE_* env is set, so the file is the source of truth.
ACTIVE_RENDERER: ConfigRenderer = JsonFileRenderer()

__all__ = ["ACTIVE_RENDERER", "ConfigRenderer", "EnvRenderer", "JsonFileRenderer"]
