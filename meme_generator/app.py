import json
import os
import secrets
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal, Optional

import filetype
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Form,
    Header,
    HTTPException,
    Response,
    UploadFile,
)
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ValidationError

from meme_generator.compat import model_dump, model_json_schema, type_validate_python
from meme_generator.config import meme_config
from meme_generator.exception import (
    ArgModelMismatch,
    MemeGeneratorException,
    NoSuchMeme,
)
from meme_generator.log import LOGGING_CONFIG, setup_logger
from meme_generator.manager import get_meme, get_meme_keys, get_memes, reload_memes
from meme_generator.meme import CommandShortcut, Meme, MemeArgsModel, ParserOption
from meme_generator.utils import MemeProperties, render_meme_list, run_sync
from meme_generator.version import __version__

STATIC_DIR = Path("data/memes/static")
RELOAD_COOLDOWN_SECONDS = 30
_reload_next_allowed = 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    publish_memes(get_memes())
    yield


app = FastAPI(lifespan=lifespan)
_generation_routes = []
_static_documents: dict[str, str] = {}


class MemeArgsResponse(BaseModel):
    args_model: dict[str, Any]
    args_examples: list[dict[str, Any]]
    parser_options: list[ParserOption]


class MemeParamsResponse(BaseModel):
    min_images: int
    max_images: int
    min_texts: int
    max_texts: int
    default_texts: list[str]
    args_type: Optional[MemeArgsResponse] = None


class MemeInfoResponse(BaseModel):
    key: str
    params_type: MemeParamsResponse
    keywords: list[str]
    shortcuts: list[CommandShortcut]
    tags: set[str]
    date_created: datetime
    date_modified: datetime


def register_router(meme: Meme, router: APIRouter):
    if args_type := meme.params_type.args_type:
        args_model = args_type.args_model
    else:
        args_model = MemeArgsModel

    def args_checker(
        args: Optional[str] = Form(default=json.dumps(model_dump(args_model()))),
    ):
        if not args:
            return MemeArgsModel()
        try:
            model = type_validate_python(args_model, json.loads(args))
        except ValidationError as e:
            e = ArgModelMismatch(str(e))
            raise HTTPException(status_code=552, detail=e.message)
        return model

    @router.post(f"/memes/{meme.key}/")
    async def _(
        images: list[UploadFile] = [],
        texts: list[str] = meme.params_type.default_texts,
        args: args_model = Depends(args_checker),  # type: ignore
    ):
        imgs: list[bytes] = []
        for image in images:
            imgs.append(await image.read())

        texts = [text for text in texts if text]

        assert isinstance(args, args_model)

        try:
            result = await run_sync(meme)(
                images=imgs, texts=texts, args=model_dump(args)
            )
        except MemeGeneratorException as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)

        content = result.getvalue()
        media_type = str(filetype.guess_mime(content)) or "text/plain"
        return Response(content=content, media_type=media_type)


class MemeKeyWithProperties(BaseModel):
    meme_key: str
    disabled: bool = False
    labels: list[Literal["new", "hot"]] = []


class RenderMemeListRequest(BaseModel):
    meme_list: Optional[list[MemeKeyWithProperties]] = None
    text_template: str = "{keywords}"
    add_category_icon: bool = True


def meme_info(meme: Meme) -> MemeInfoResponse:
    args_type_response = None
    if args_type := meme.params_type.args_type:
        args_model = args_type.args_model
        args_type_response = MemeArgsResponse(
            args_model=model_json_schema(args_model),
            args_examples=[model_dump(example) for example in args_type.args_examples],
            parser_options=args_type.parser_options,
        )

    return MemeInfoResponse(
        key=meme.key,
        params_type=MemeParamsResponse(
            min_images=meme.params_type.min_images,
            max_images=meme.params_type.max_images,
            min_texts=meme.params_type.min_texts,
            max_texts=meme.params_type.max_texts,
            default_texts=meme.params_type.default_texts,
            args_type=args_type_response,
        ),
        keywords=meme.keywords,
        shortcuts=meme.shortcuts,
        tags=meme.tags,
        date_created=meme.date_created,
        date_modified=meme.date_modified,
    )


def register_routers():
    if getattr(app.state, "routers_registered", False):
        return
    app.state.routers_registered = True

    @app.get("/memes/static/infos.json")
    async def infos_document():
        return Response(
            _static_documents["infos.json"],
            media_type="application/json",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/memes/static/keyMap.json")
    async def keywords_document():
        return Response(
            _static_documents["keyMap.json"],
            media_type="application/json",
            headers={"Cache-Control": "no-cache"},
        )

    @app.post("/memes/reload")
    async def reload_resources(authorization: Optional[str] = Header(default=None)):
        global _reload_next_allowed
        token = os.environ.get("MEME_RELOAD_TOKEN", "")
        if not token:
            raise HTTPException(status_code=503, detail="Meme reload is disabled")
        scheme, _, supplied_token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            supplied_token.encode("utf-8"), token.encode("utf-8")
        ):
            raise HTTPException(
                status_code=401,
                detail="Invalid reload token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        now = monotonic()
        if now < _reload_next_allowed:
            raise HTTPException(
                status_code=429,
                detail="Meme reload is cooling down",
                headers={"Retry-After": str(int(_reload_next_allowed - now) + 1)},
            )
        # Reserve the interval before loading so failed attempts are limited too.
        # Reload runs without yielding on the single server event loop.
        _reload_next_allowed = now + RELOAD_COOLDOWN_SECONDS
        try:
            with reload_memes() as candidate:
                publish_memes(list(candidate.values()))
        except Exception as exc:
            from meme_generator.log import logger

            logger.exception("Failed to reload memes")
            raise HTTPException(
                status_code=500, detail="Meme reload failed; previous registry retained"
            ) from exc
        return {"success": True, "count": len(candidate)}

    @app.post("/memes/render_list")
    def _(params: RenderMemeListRequest = RenderMemeListRequest()):
        try:
            meme_list = [
                (
                    get_meme(p.meme_key),
                    MemeProperties(disabled=p.disabled, labels=p.labels),
                )
                for p in (
                    params.meme_list
                    if params.meme_list is not None
                    else [
                        MemeKeyWithProperties(meme_key=m.key)
                        for m in sorted(get_memes(), key=lambda m: m.key)
                    ]
                )
            ]
        except NoSuchMeme as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)

        result = render_meme_list(
            meme_list,
            text_template=params.text_template,
            add_category_icon=params.add_category_icon,
        )
        content = result.getvalue()
        media_type = str(filetype.guess_mime(content)) or "text/plain"
        return Response(content=content, media_type=media_type)

    @app.get("/meme/version")
    def _():
        return __version__

    @app.get("/memes/keys")
    def _():
        return get_meme_keys()

    @app.get("/memes/{key}/info")
    def _(key: str):
        try:
            meme = get_meme(key)
        except NoSuchMeme as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)

        return meme_info(meme)

    @app.get("/memes/{key}/preview")
    async def _(key: str):
        try:
            meme = get_meme(key)
            result = await run_sync(meme.generate_preview)()
        except MemeGeneratorException as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)

        content = result.getvalue()
        media_type = str(filetype.guess_mime(content)) or "text/plain"
        return Response(content=content, media_type=media_type)


def publish_memes(memes: list[Meme]):
    """Prepare routes and both documents before replacing the active snapshot."""
    global _generation_routes, _static_documents
    router = APIRouter()
    infos = {}
    key_map = {}
    for meme in sorted(memes, key=lambda meme: meme.key):
        register_router(meme, router)
        infos[meme.key] = jsonable_encoder(meme_info(meme))
        for keyword in meme.keywords:
            key_map[keyword] = meme.key
    documents = {
        "infos.json": json.dumps(infos, ensure_ascii=False),
        "keyMap.json": json.dumps(key_map, ensure_ascii=False),
    }
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    staged = {}
    previous = {}
    replaced = []
    try:
        for name, content in documents.items():
            destination = STATIC_DIR / name
            previous[name] = destination.read_bytes() if destination.exists() else None
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=STATIC_DIR, delete=False
            ) as temporary:
                staged[name] = Path(temporary.name)
                temporary.write(content)
        for name, temporary in staged.items():
            temporary.replace(STATIC_DIR / name)
            replaced.append(name)
    except BaseException:
        for name in replaced:
            destination = STATIC_DIR / name
            if previous[name] is None:
                destination.unlink()
            else:
                destination.write_bytes(previous[name])
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
    app.router.routes = [
        route for route in app.router.routes if route not in _generation_routes
    ] + router.routes
    _generation_routes = list(router.routes)
    _static_documents = documents
    app.openapi_schema = None


register_routers()


def run_server():
    import uvicorn

    register_routers()
    uvicorn.run(
        app,
        host=meme_config.server.host,
        port=meme_config.server.port,
        log_config=LOGGING_CONFIG,
    )


if __name__ == "__main__":
    setup_logger()
    run_server()
