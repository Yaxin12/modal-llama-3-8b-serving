# ---
# cmd: ["modal", "deploy", "06_gpu_and_ml/llm-serving/vllm_oai_compatible/api.py", "&&", "pip", "install", "openai==1.13.3", "&&" "python", "06_gpu_and_ml/llm-serving/vllm_oai_compatible/client.py"]
# ---
# # Run an OpenAI-Compatible vLLM Server
#
# LLMs do more than just model language: they chat, they produce JSON and XML, they run code, and more.
# OpenAI's API has emerged as a standard interface for LLMs,
# and it is supported by open source LLM serving frameworks like vLLM.
#
# In this example, we show how to run a vLLM server in OpenAI-compatible mode on Modal.
# Note that the vLLM server is a FastAPI app, which can be configured and extended just like any other.
# Here, we use it to add simple authentication middleware, following the
# [implementation in the vLLM repository](https://github.com/vllm-project/vllm/blob/v0.4.1/vllm/entrypoints/openai/api_server.py).
#
# ## Set up the container image
#
# Our first order of business is to define the environment our server will run in: the [container `Image`](https://modal.com/docs/guide/custom-container).
# We'll build it up, step-by-step, from a slim Debian Linux image.
#
# First, we install some dependencies with `pip`.

from pathlib import Path

import modal
import os

vllm_image = modal.Image.debian_slim(python_version="3.10").pip_install(
    [
        "vllm==0.5.3.post1",  # LLM serving
        "huggingface_hub==0.24.2",  # download models from the Hugging Face Hub
        "hf-transfer==0.1.8",  # download models faster
    ]
)

# Then, we need to get hold of the weights for the model we're serving:
# Meta's LLaMA 3-8B Instruct. We create a Python function for this and add it to the image definition,
# so that we only need to download it when we define the image, not every time we run the server.
#
# If you adapt this example to run another model,
# note that for this step to work on a [gated model](https://huggingface.co/docs/hub/en/models-gated)
# the `HF_TOKEN` environment variable must be set and provided as a [Modal Secret](https://modal.com/secrets).


MODEL_NAME = "NousResearch/Meta-Llama-3.1-8B-Instruct"
MODEL_REVISION = "d10aef7999a2b5ba950ab3974312feeedbfe0b77"
MODEL_DIR = f"/models/{MODEL_NAME}"


def download_model_to_image(model_dir, model_name, model_revision):
    import os

    from huggingface_hub import snapshot_download

    os.makedirs(model_dir, exist_ok=True)

    snapshot_download(
        model_name,
        local_dir=model_dir,
        ignore_patterns=["*.pt", "*.bin"],  # Using safetensors
        revision=model_revision,
        use_auth_token=os.environ["HF_TOKEN"]
    )


MINUTES = 60

vllm_image = vllm_image.env({"HF_HUB_ENABLE_HF_TRANSFER": "1"}).run_function(
    download_model_to_image,
    secrets=[modal.Secret.from_name("llama-huggingface-secret")],
    timeout=20 * MINUTES,
    kwargs={
        "model_dir": MODEL_DIR,
        "model_name": MODEL_NAME,
        "model_revision": MODEL_REVISION,
    },
)

# ## Build the server
#
# vLLM's OpenAI-compatible server is a [FastAPI](https://fastapi.tiangolo.com/) app.
#
# FastAPI is a Python web framework that implements the [ASGI standard](https://en.wikipedia.org/wiki/Asynchronous_Server_Gateway_Interface),
# much like [Flask](https://en.wikipedia.org/wiki/Flask_(web_framework)) is a Python web framework
# that implements the [WSGI standard](https://en.wikipedia.org/wiki/Web_Server_Gateway_Interface).
#
# Modal offers [first-class support for ASGI (and WSGI) apps](https://modal.com/docs/guide/webhooks). We just need to decorate a function that returns the app
# with `@modal.asgi_app()` (or `@modal.wsgi_app()`) and then add it to the Modal app with the `app.function` decorator.
#
# The function below first imports the FastAPI app from the vLLM library, then adds some middleware. You might also add more routes here.
#
# Then, the function creates an `AsyncLLMEngine`, the core of the vLLM server. It's responsible for loading the model, running inference, and serving responses.
#
# After attaching that engine to the FastAPI app via the `api_server` module of the vLLM library, we return the FastAPI app
# so it can be served on Modal.

app = modal.App("vllm-openai-compatible")

N_GPU = 1  # tip: for best results, first upgrade to A100s or H100s, and only then increase GPU count

local_template_path = (
    Path(__file__).parent / "template_llama3.jinja"
)  # many models have a custom chat template -- using the wrong one subtly degrades results. watch out for it!

@app.function(
    image=vllm_image,
    gpu=modal.gpu.A10G(count=N_GPU),
    container_idle_timeout=20 * MINUTES,
    secrets=[modal.Secret.from_name("dsba-llama3-key")],
    mounts=[
        modal.Mount.from_local_file(
            local_template_path, remote_path="/root/chat_template.jinja"
        )
    ],
)
@modal.asgi_app()
def serve():
    import asyncio

    import fastapi
    import vllm.entrypoints.openai.api_server as api_server
    from vllm.engine.arg_utils import AsyncEngineArgs
    from vllm.engine.async_llm_engine import AsyncLLMEngine
    from vllm.entrypoints.openai.serving_chat import OpenAIServingChat
    from vllm.entrypoints.openai.serving_completion import (
        OpenAIServingCompletion,
    )
    from vllm.entrypoints.logger import RequestLogger
    from vllm.usage.usage_lib import UsageContext

    # create a fastAPI app that uses vLLM's OpenAI-compatible router
    app = fastapi.FastAPI(
        title=f"OpenAI-compatible {MODEL_NAME} server",
        description="Run an OpenAI-compatible LLM server with vLLM on modal.com",
        version="0.0.1",
        docs_url="/docs",
    )

    # security: CORS middleware for external requests
    http_bearer = fastapi.security.HTTPBearer(
        scheme_name="Bearer Token", description="See code for authentication details."
    )
    app.add_middleware(
        fastapi.middleware.cors.CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # security: inject dependency on authed routes
    async def is_authenticated(api_key: str = fastapi.Security(http_bearer)):
        if api_key.credentials != os.environ["DSBA_LLAMA3_KEY"]:
            raise fastapi.HTTPException(
                status_code=fastapi.status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
            )
        return {"username": "authenticated_user"}

    router = fastapi.APIRouter(dependencies=[fastapi.Depends(is_authenticated)])

    router.include_router(api_server.router)
    app.include_router(router)

    engine_args = AsyncEngineArgs(
        model=MODEL_DIR,
        tensor_parallel_size=N_GPU,
        gpu_memory_utilization=0.90,
        max_model_len=1024 + 128,
        enforce_eager=True,
    )

    engine = AsyncLLMEngine.from_engine_args(
        engine_args, usage_context=UsageContext.OPENAI_API_SERVER
    )

    try:  # copied from vLLM -- https://github.com/vllm-project/vllm/blob/507ef787d85dec24490069ffceacbd6b161f4f72/vllm/entrypoints/openai/api_server.py#L235C1-L247C1
        event_loop = asyncio.get_running_loop()
    except RuntimeError:
        event_loop = None

    if event_loop is not None and event_loop.is_running():
        # If the current is instanced by Ray Serve,
        # there is already a running event loop
        model_config = event_loop.run_until_complete(engine.get_model_config())
    else:
        # When using single vLLM without engine_use_ray
        model_config = asyncio.run(engine.get_model_config())

    request_logger = RequestLogger(max_log_len=2048)

    api_server.openai_serving_chat = OpenAIServingChat(
        engine,
        model_config=model_config,
        served_model_names=[MODEL_DIR],
        chat_template=None,
        response_role="assistant",
        lora_modules=[],
        prompt_adapters=[],
        request_logger=request_logger,
    )
    api_server.openai_serving_completion = OpenAIServingCompletion(
        engine,
        model_config=model_config,
        served_model_names=[MODEL_DIR],
        lora_modules=[],
        prompt_adapters=[],
        request_logger=request_logger,
    )

    return app


# ## Deploy the server
#
# To deploy the API on Modal, just run
# ```bash
# modal deploy api.py
# ```
#
# This will create a new app on Modal, build the container image for it, and deploy.
#
# ### Interact with the server
#
# Once it is deployed, you'll see a URL appear in the command line,
# something like `https://your-workspace-name--vllm-openai-compatible-serve.modal.run`.
#
# You can find [interactive Swagger UI docs](https://swagger.io/tools/swagger-ui/)
# at the `/docs` route of that URL, i.e. `https://your-workspace-name--vllm-openai-compatible-serve.modal.run/docs`.
# These docs describe each route and indicate the expected input and output.
#
# For simple routes like `/health`, which checks whether the server is responding,
# you can even send a request directly from the docs.
#
# To interact with the API programmatically, you can use the Python `openai` library.
#
# See the small test `client.py` script included with this example for details.
#
# ```bash
# # pip install openai==1.13.3
# python client.py
# ```