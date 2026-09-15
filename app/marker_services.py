"""A marker LLM service for Ornith-35B, loaded by `marker_single` itself.

NOT part of the FastAPI app -- `--llm_service app.marker_services.OrnithService`
(app/tools/marker.py's `_extra_args`) is resolved by marker's own config machinery
(`marker.util.strings_to_classes`, a plain `importlib.import_module`) inside
`marker_run.sh`'s subprocess, which runs under `~/venvs/marker`'s interpreter --
a completely different venv from this app's (`~/venvs/workbench`), with no fastapi
installed. `app/__init__.py` is empty for exactly this reason: importing this module
must never pull in `app.main` or anything else that needs fastapi. `marker.convert()`
puts this repo's root on `PYTHONPATH` for the subprocess so `app.marker_services`
resolves at all.

Everything below is marker's own `marker.services.openai.OpenAIService.__call__`
(marker 2026-09 install), copied rather than wrapped because the API call is inlined
in the middle of its retry loop with no seam to override. Check this against
`marker/services/openai.py` in ~/venvs/marker on any marker upgrade.
"""

from __future__ import annotations

import json
import time
from typing import List

import PIL
from marker.logger import get_logger
from marker.schema.blocks import Block
from marker.services.openai import OpenAIService
from openai import APITimeoutError, RateLimitError
from pydantic import BaseModel

logger = get_logger()


class OrnithService(OpenAIService):
    """OpenAIService, with every call forced into Ornith's non-thinking mode.

    MEASURED in ../halo-prep (config/llama-ornith35.service, config/llama-ornith.service,
    docs/11-model-switch-inventory.md): on a structured-extraction prompt, Ornith answers
    in 31 completion tokens / 0 reasoning chars with
    `chat_template_kwargs={"enable_thinking": false}`, against 194 / 768 with no
    override -- and it silently ignores the {"thinking": false} spelling some other
    models use. Confirmed by the operator (2026-09-15) that Ornith performs best as a
    VLM with thinking off. marker's stock OpenAIService has no `extra_body` of its own,
    so this is the whole point of the subclass.
    """

    def __call__(
        self,
        prompt: str,
        image: PIL.Image.Image | List[PIL.Image.Image] | None,
        block: Block | None,
        response_schema: type[BaseModel],
        max_retries: int | None = None,
        timeout: int | None = None,
    ):
        if max_retries is None:
            max_retries = self.max_retries
        if timeout is None:
            timeout = self.timeout

        client = self.get_client()
        image_data = self.format_image_for_llm(image)

        messages = [
            {
                "role": "user",
                "content": [
                    *image_data,
                    {"type": "text", "text": prompt},
                ],
            }
        ]

        total_tries = max_retries + 1
        for tries in range(1, total_tries + 1):
            try:
                response = client.chat.completions.parse(
                    extra_headers={
                        "X-Title": "Marker",
                        "HTTP-Referer": "https://github.com/datalab-to/marker",
                    },
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                    model=self.openai_model,
                    messages=messages,
                    timeout=timeout,
                    response_format=response_schema,
                )
                response_text = response.choices[0].message.content
                total_tokens = response.usage.total_tokens
                if block:
                    block.update_metadata(
                        llm_tokens_used=total_tokens, llm_request_count=1
                    )
                return json.loads(response_text)
            except (APITimeoutError, RateLimitError) as e:
                if tries == total_tries:
                    logger.error(
                        f"Rate limit error: {e}. Max retries reached. Giving up. "
                        f"(Attempt {tries}/{total_tries})",
                    )
                    break
                wait_time = tries * self.retry_wait_time
                logger.warning(
                    f"Rate limit error: {e}. Retrying in {wait_time} seconds... "
                    f"(Attempt {tries}/{total_tries})",
                )
                time.sleep(wait_time)
            except Exception as e:
                logger.error(f"OpenAI inference failed: {e}")
                break

        return {}
