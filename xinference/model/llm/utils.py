# Copyright 2022-2023 XProbe Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import functools
import json
import logging
import re
import time
import typing
import uuid
from io import BytesIO
from typing import (
    Any,
    AsyncGenerator,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
    cast,
)

import requests
from PIL import Image

from ...types import (
    ChatCompletion,
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatCompletionMessage,
    Completion,
    CompletionChoice,
    CompletionChunk,
    CompletionUsage,
)
from .core import chat_context_var
from .reasoning_parser import ReasoningParser

logger = logging.getLogger(__name__)


QWEN_TOOL_CALL_FAMILY = [
    "qwen1.5-chat",
    "qwen1.5-moe-chat",
    "qwen2-instruct",
    "qwen2-moe-instruct",
    "qwen2.5-instruct",
    "qwen2.5-coder-instruct",
    "XiYanSQL-QwenCoder-2504",
    "QwQ-32B",
    "qwen3",
    "HuatuoGPT-o1-Qwen2.5",
    "DianJin-R1",
]

GLM4_TOOL_CALL_FAMILY = [
    "glm4-chat",
    "glm4-chat-1m",
]

LLAMA3_TOOL_CALL_FAMILY = [
    "llama-3.1-instruct",
    "HuatuoGPT-o1-LLaMA-3.1",
]

DEEPSEEK_TOOL_CALL_FAMILY = [
    "deepseek-v3",
]

TOOL_CALL_FAMILY = (
    QWEN_TOOL_CALL_FAMILY
    + GLM4_TOOL_CALL_FAMILY
    + LLAMA3_TOOL_CALL_FAMILY
    + DEEPSEEK_TOOL_CALL_FAMILY
)

QWEN_TOOL_CALL_SYMBOLS = ["<tool_call>", "</tool_call>"]


class ChatModelMixin:
    @staticmethod
    @functools.lru_cache
    def _compile_jinja_template(chat_template):
        """
        Copied from transformers source code.
        """
        try:
            from jinja2.exceptions import TemplateError
            from jinja2.sandbox import ImmutableSandboxedEnvironment
        except ImportError:
            raise ImportError("xinference requires jinja2 to be installed.")

        def raise_exception(message):
            raise TemplateError(message)

        jinja_env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
        jinja_env.globals["raise_exception"] = raise_exception
        return jinja_env.from_string(chat_template)

    def _build_from_raw_template(
        self, messages: List, chat_template: str, **kwargs
    ) -> str:
        compiled_template = self._compile_jinja_template(chat_template)
        rendered = compiled_template.render(
            messages=messages, add_generation_prompt=True, **kwargs
        )
        return rendered

    def get_full_context(
        self,
        messages: List,
        chat_template: str,
        tokenizer=None,
        tokenize=False,
        **kwargs,
    ):
        if "vision" not in self.model_family.model_ability:  # type: ignore
            messages = self.convert_messages_with_content_list_to_str_conversion(
                messages
            )
        if tokenizer is not None:
            try:
                full_context = tokenizer.apply_chat_template(
                    messages,
                    tokenize=tokenize,
                    chat_template=chat_template,
                    add_generation_prompt=True,
                    **kwargs,
                )
                logger.debug("Prompt: %s", full_context)
                return full_context
            except Exception as e:
                logger.warning(
                    f"tokenizer.apply_chat_template error. Maybe this is an old model: {e}"
                )
                return self._build_from_raw_template(messages, chat_template, **kwargs)
        else:
            # build from jinja
            # Compilation function uses a cache to avoid recompiling the same template
            return self._build_from_raw_template(messages, chat_template, **kwargs)

    @staticmethod
    def _get_chat_template_kwargs_from_generate_config(
        generate_config: Optional[Union[dict, Any]],
        reasoning_parser: Optional[ReasoningParser] = None,
    ) -> Optional[dict]:
        if generate_config and "chat_template_kwargs" in generate_config:
            kwargs = generate_config["chat_template_kwargs"]
            if isinstance(kwargs, str):
                try:
                    return json.loads(kwargs)
                except json.JSONDecodeError:
                    raise TypeError(
                        f"`chat_template_kwargs` should be json parsable, "
                        f"got: {kwargs}"
                    )
            elif isinstance(kwargs, dict):
                return kwargs
            else:
                raise TypeError(
                    f"`chat_template_kwargs` but be a JSON parsable str "
                    f"or dict, got: {kwargs}"
                )
        elif reasoning_parser and not reasoning_parser.enable_thinking:
            # hybrid model like qwen3,
            # disabled thinking
            return {"enable_thinking": False}
        return None

    @staticmethod
    def convert_messages_with_content_list_to_str_conversion(
        messages: List[Dict],
    ) -> List[Dict]:
        """
        Handles messages with content list conversion, in order to support Cline, see GH#2659 .
        """
        for message in messages:
            texts = ""
            msg_content = message.get("content")
            if msg_content:
                if isinstance(msg_content, str):
                    texts = msg_content
                elif isinstance(msg_content, list):
                    texts = "\n".join(item.get("text", "") for item in msg_content)
            if texts:
                message["content"] = texts
        return messages

    @staticmethod
    def get_specific_prompt(model_family: str, messages: List[ChatCompletionMessage]):
        """
        Inspired by FastChat. Format chat history into a prompt according to the prompty style of
        different models.
        """
        _messages = [x for x in messages]  # copy for not modifying the origin messages
        _messages.append({"role": "assistant", "content": ""})

        if "internvl" in model_family.lower():
            system_prompt = (
                messages[0]["content"] if messages[0]["role"] == "system" else ""
            )
            intra_message_sep = "<|im_end|>"
            ret = (
                "<s>"
                if system_prompt == ""
                else "<s><|im_start|>system\n"  # type: ignore
                + system_prompt
                + intra_message_sep
                + "\n"
            )
            images = []  # type: ignore
            for message in _messages:
                role = "<|im_start|>" + message["role"]
                content = message["content"]
                if isinstance(content, str):
                    if content:
                        ret += role + "\n" + content + intra_message_sep + "\n"
                    else:
                        ret += role + "\n"
                elif isinstance(content, list):
                    text = ""
                    image_urls = []
                    for c in content:
                        c_type = c.get("type")
                        if c_type == "text":
                            text = c["text"]
                        elif c_type == "image_url":
                            image_urls.append(c["image_url"]["url"])
                    image_futures = []
                    from concurrent.futures import ThreadPoolExecutor

                    with ThreadPoolExecutor() as executor:
                        for image_url in image_urls:
                            fut = executor.submit(_decode_image, image_url)
                            image_futures.append(fut)
                    images.extend([fut.result() for fut in image_futures])
                    if len(image_futures) == 0:
                        ret += role + "\n" + text + intra_message_sep + "\n"
                    else:
                        placeholders = "\n".join(
                            f"Image-{i+1}: <image>\n"
                            for i in range(
                                len(images) - len(image_futures), len(images)
                            )
                        )
                        ret += (
                            role
                            + "\n"
                            + f"{placeholders}\n{text}"
                            + intra_message_sep
                            + "\n"
                        )
            if len(images) == 1:
                ret = ret.replace("Image-1: <image>\n", "<image>\n")
            return ret, images
        else:
            raise ValueError(f"Invalid model family: {model_family}")

    @classmethod
    def _to_chat_completion_chunk(
        cls,
        chunk: CompletionChunk,
        reasoning_parser: Optional[ReasoningParser] = None,
        previous_texts: Optional[List[str]] = None,
    ) -> ChatCompletionChunk:
        choices = chunk.get("choices")
        if (
            chunk.get("object") == "chat.completion.chunk"
            and choices
            and "delta" in choices[0]
        ):
            if choices[0]["finish_reason"] is None:
                if reasoning_parser and reasoning_parser.check_content_parser():
                    # process parsing reasoning content
                    assert previous_texts is not None
                    delta = choices[0]["delta"]  # type: ignore
                    if text := delta.get("content"):
                        current_text = previous_texts[-1] + text
                        delta = reasoning_parser.extract_reasoning_content_streaming(
                            previous_text=previous_texts[-1],
                            current_text=current_text,
                            delta_text=text,
                        )
                        previous_texts[-1] = current_text
                        choices[0]["delta"] = delta  # type: ignore
            elif choices[0]["finish_reason"] is not None:
                delta = choices[0]["delta"]  # type: ignore
                if "content" not in delta:
                    delta["content"] = ""  # type: ignore
                if reasoning_parser and reasoning_parser.check_content_parser():
                    delta["reasoning_content"] = None  # type: ignore
            # Already a ChatCompletionChunk, we don't need to convert chunk.
            return cast(ChatCompletionChunk, chunk)

        choices_list = []
        for i, choice in enumerate(choices):  # type: ignore
            delta = ChatCompletionChunkDelta()
            if "text" in choice and choice["finish_reason"] is None:
                if reasoning_parser and reasoning_parser.check_content_parser():
                    assert previous_texts is not None
                    current_text = previous_texts[-1] + choice["text"]
                    delta = reasoning_parser.extract_reasoning_content_streaming(
                        previous_text=previous_texts[-1],
                        current_text=current_text,
                        delta_text=choice["text"],
                    )
                    previous_texts[-1] = current_text
                else:
                    delta["content"] = choice["text"]
            elif "text" in choice and choice["finish_reason"] is not None:
                delta["content"] = choice["text"]
                if reasoning_parser and reasoning_parser.check_content_parser():
                    delta["reasoning_content"] = None
            elif "tool_calls" in choice:
                delta["tool_calls"] = choice["tool_calls"]
            choices_list.append(
                {
                    "index": i,
                    "delta": delta,
                    "finish_reason": choice["finish_reason"],
                }
            )
        assert choices is not None
        usage = (
            chunk["usage"]
            if choices[0]["finish_reason"] is not None
            and reasoning_parser
            and reasoning_parser.check_content_parser()
            else None
        )
        chat_chunk = {
            "id": "chat" + chunk["id"],
            "model": chunk["model"],
            "created": chunk["created"],
            "object": "chat.completion.chunk",
            "choices": choices_list,
            "usage": usage,
        }
        return cast(ChatCompletionChunk, chat_chunk)

    @classmethod
    def _get_first_chat_completion_chunk(
        cls,
        chunk: CompletionChunk,
        reasoning_parser: Optional[ReasoningParser] = None,
    ) -> List[ChatCompletionChunk]:
        choices_list: List[ChatCompletionChunkChoice] = []
        chunks: List[ChatCompletionChunk] = []
        for i, choice in enumerate(chunk["choices"]):
            delta = ChatCompletionChunkDelta(role="assistant", content="")
            if reasoning_parser and reasoning_parser.check_content_parser():
                delta["content"] = None
                delta["reasoning_content"] = ""
            choices_list.append(
                ChatCompletionChunkChoice(
                    index=i,
                    delta=delta,
                    finish_reason=None,
                )
            )
        chat_chunk = ChatCompletionChunk(
            id="chat" + chunk["id"],
            model=chunk["model"],
            created=chunk["created"],
            object="chat.completion.chunk",
            choices=choices_list,
        )
        chunks.append(chat_chunk)
        if reasoning_parser:
            chunks.extend(reasoning_parser.prepare_first_reasoning_content_chunk(chunk))
        return chunks

    @classmethod
    def _get_final_chat_completion_chunk(
        cls, chunk: CompletionChunk
    ) -> ChatCompletionChunk:
        chat_chunk = {
            "id": "chat" + chunk["id"],
            "model": chunk["model"],
            "created": chunk["created"],
            "object": "chat.completion.chunk",
            "choices": [],
        }
        usage = chunk.get("usage")
        if usage is not None:
            chat_chunk["usage"] = usage
        return cast(ChatCompletionChunk, chat_chunk)

    @classmethod
    def _to_chat_completion_chunks(
        cls,
        chunks: Iterator[CompletionChunk],
        reasoning_parse: Optional[ReasoningParser] = None,
    ) -> Iterator[ChatCompletionChunk]:
        previous_texts = [""]
        if reasoning_parse:
            chunks = reasoning_parse.prepare_reasoning_content_sync(chunks)
        for _, chunk in enumerate(chunks):
            # usage
            choices = chunk.get("choices")
            if not choices:
                yield cls._get_final_chat_completion_chunk(chunk)
            else:
                r = cls._to_chat_completion_chunk(
                    chunk, reasoning_parse, previous_texts
                )
                yield r

    @classmethod
    def _tools_to_messages_for_deepseek(
        cls, messages: List[dict], tools: Iterable[dict]
    ):
        # deepseek integrates tool calls into messages
        # we follow the chat template rule to integrate tools into messages
        tool_call_message: Dict[str, Any] = {
            "role": "assistant",
            "content": None,
            "tool_calls": [],
        }

        for tool in tools:
            function_name = tool["function"]["name"]
            parameters = tool["function"].get("parameters", {}).get("properties", {})
            function_args_json = json.dumps(parameters)

            tool_call_message["tool_calls"].append(
                {
                    "type": "function",
                    "function": {
                        "name": function_name,
                        "arguments": function_args_json,
                    },
                }
            )

        messages.append(tool_call_message)

    @classmethod
    async def _async_to_chat_completion_chunks(
        cls,
        chunks: AsyncGenerator[CompletionChunk, None],
        reasoning_parser: Optional[ReasoningParser] = None,
        ctx: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[ChatCompletionChunk, None]:
        def set_context():
            if ctx:
                chat_context_var.set(ctx)

        previous_texts = [""]
        # Process chunks
        if reasoning_parser:
            set_context()
            chunks = reasoning_parser.prepare_reasoning_content_streaming(chunks)
        async for chunk in chunks:
            set_context()
            choices = chunk.get("choices")
            if not choices:
                # usage
                chat_chunk = cls._get_final_chat_completion_chunk(chunk)
            else:
                chat_chunk = cls._to_chat_completion_chunk(
                    chunk, reasoning_parser, previous_texts
                )
            yield chat_chunk

    @staticmethod
    def _to_chat_completion(
        completion: Completion, reasoning_parser: Optional[ReasoningParser] = None
    ) -> ChatCompletion:
        # prepare reasoning content
        if reasoning_parser:
            completion = reasoning_parser.prepare_reasoning_content(completion)

        if completion.get("object") == "chat.completion" and completion.get("choices"):
            # Already a ChatCompletion
            for choice in completion["choices"]:
                message = choice["message"]  # type: ignore
                text = message["content"]  # Original content from the message

                if reasoning_parser and reasoning_parser.check_content_parser():
                    # Parse into reasoning and content parts
                    (
                        reasoning_val,
                        content_val,
                    ) = reasoning_parser.extract_reasoning_content(text)
                    message["content"] = content_val
                    if reasoning_val is not None:
                        message["reasoning_content"] = reasoning_val
            return cast(ChatCompletion, completion)

        choices = []
        for i, choice in enumerate(completion["choices"]):
            content = choice["text"]
            reasoning_content = None

            if reasoning_parser and reasoning_parser.check_content_parser():
                reasoning_content, content = reasoning_parser.extract_reasoning_content(  # type: ignore
                    choice
                )

            message = {"role": "assistant", "content": content}

            # add only reasoning_content is None
            if reasoning_content is not None:
                message["reasoning_content"] = reasoning_content

            choices.append(
                {
                    "index": i,
                    "message": message,
                    "finish_reason": choice["finish_reason"],
                }
            )
        return {
            "id": "chat" + completion["id"],
            "object": "chat.completion",
            "created": completion["created"],
            "model": completion["model"],
            "choices": choices,  # type: ignore
            "usage": completion["usage"],
        }

    @staticmethod
    def _eval_glm_chat_arguments(c) -> List[Tuple]:
        """
        Currently, glm4 tool call only supports one function
        """
        try:
            if isinstance(c, dict):
                try:
                    return [(None, c["name"], json.loads(c["arguments"]))]
                except Exception:
                    return [(None, c["name"], c["arguments"])]
        except KeyError:
            logger.error("Can't parse glm output: %s", c)
            return [(str(c), None, None)]
        else:
            return [(str(c), None, None)]

    @classmethod
    def _handle_qwen_tool_result(cls, text: str) -> List[Tuple]:
        text: str = text.strip()  # type: ignore

        def split_into_blocks(text: str) -> list[str]:
            # Match blocks starting with <think> or <tool_call> and ending with </think> or </tool_call>
            pattern = r"(<(think|tool_call)>.*?</\2>)"
            parts = []
            last_end = 0
            # Find all label blocks and record their positions
            for m in re.finditer(pattern, text, re.DOTALL):
                # Text before adding tags
                if m.start() > last_end:
                    parts.append(text[last_end : m.start()])
                # Add label block
                parts.append(m.group(0))
                last_end = m.end()
            # Text after adding the last tag
            if last_end < len(text):
                parts.append(text[last_end:])
            return parts

        contents = split_into_blocks(text)
        results: List[Tuple] = []
        for content in contents:
            if content.strip():
                pos1 = content.find(QWEN_TOOL_CALL_SYMBOLS[0])
                if pos1 != -1:
                    content = content[pos1 + len(QWEN_TOOL_CALL_SYMBOLS[0]) :]
                pos2 = content.find(QWEN_TOOL_CALL_SYMBOLS[1])
                if pos2 != -1:
                    content = content[:pos2]
                try:
                    res = json.loads(content, strict=False)
                    results.append((None, res["name"], res["arguments"]))
                except Exception as e:
                    logger.error(
                        "Can't parse single qwen tool call output: %s. Error: %s",
                        content,
                        e,
                    )
                    results.append((content, None, None))
        return results

    @classmethod
    def _eval_qwen_chat_arguments(
        cls, c, tool_call_text: Optional[str] = None
    ) -> List[Tuple]:
        text = c["choices"][0]["text"]
        if tool_call_text:
            text = tool_call_text
        return cls._handle_qwen_tool_result(text)

    @classmethod
    def _eval_llama3_chat_arguments(cls, c) -> List[Tuple]:
        text = c["choices"][0]["text"]
        try:
            data = eval(text, {}, {})
            return [(None, data["name"], data["parameters"])]
        except Exception:
            return [(text, None, None)]

    @classmethod
    def _eval_deepseek_chat_arguments(cls, c) -> List[Tuple]:
        """
        Parses tool calls from deepseek-v3 format and removes duplicates.

        Returns:
        List[Tuple[Optional[str], Optional[str], Optional[dict]]]
        - (None, function_name, arguments) if successfully parsed.
        - (content, None, None) if parsing failed (content is raw JSON text).

        Example input:
        ```json
        {
            "name": "get_weather_and_time",
            "parameters": {
                "location": "Hangzhou"
            }
        }
        ```

        Output:
        [
            (None, "get_current_weather", {"location": "Hangzhou"})
        ]
        """

        text = c["choices"][0]["text"]

        pattern = r"\s*```json\s*(.*?)\s*```"
        matches = re.findall(pattern, text, re.DOTALL)

        if not matches:
            return [(text, None, None)]

        tool_calls = set()  # Used for deduplication
        results = []

        for raw_json in matches:
            func_and_args = None
            try:
                func_and_args = json.loads(raw_json)
                # Convert dictionary to frozenset for deduplication
                arguments_hashable = frozenset(func_and_args["parameters"])
                tool_call_tuple = (
                    None,
                    func_and_args["name"],
                    func_and_args["parameters"],
                )
            except json.JSONDecodeError:
                tool_call_tuple = (
                    raw_json,
                    None,
                    None,
                )  # If parsing fails, treat as raw content
                arguments_hashable = None  # No need for hashing

            # Avoid duplicate entries
            dedup_key = (
                (func_and_args["name"], arguments_hashable)
                if func_and_args is not None
                else (raw_json)
            )
            if dedup_key not in tool_calls:
                tool_calls.add(dedup_key)
                results.append(tool_call_tuple)

        return results

    @classmethod
    def _eval_tool_arguments(
        cls, model_family, c, tool_call_text: Optional[str] = None
    ):
        family = model_family.model_family or model_family.model_name
        if family in GLM4_TOOL_CALL_FAMILY:
            result = cls._eval_glm_chat_arguments(c)
        elif family in QWEN_TOOL_CALL_FAMILY:
            result = cls._eval_qwen_chat_arguments(c, tool_call_text)
        elif family in LLAMA3_TOOL_CALL_FAMILY:
            result = cls._eval_llama3_chat_arguments(c)
        elif family in DEEPSEEK_TOOL_CALL_FAMILY:
            result = cls._eval_deepseek_chat_arguments(c)
        else:
            raise Exception(
                f"Model {model_family.model_name} is not support tool calls."
            )
        logger.debug(f"Tool call content: {result}")
        return result

    @classmethod
    def _post_process_completion_chunk(
        cls,
        model_family,
        model_uid,
        c,
        chunk_id=None,
        reasoning_parser: Optional[ReasoningParser] = None,
        tool_call_text: Optional[str] = None,
    ):
        _id = chunk_id if chunk_id is not None else str(uuid.uuid4())
        tool_result = cls._eval_tool_arguments(model_family, c, tool_call_text)
        tool_calls = []
        failed_contents = []
        for content, func, args in tool_result:
            if func:
                tool_calls.append(
                    {
                        "index": 0,
                        "id": f"call_{_id}",
                        "type": "function",
                        "function": {
                            "name": func,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                )
            else:
                failed_contents.append(content)
        finish_reason = "tool_calls" if tool_calls else "stop"

        content = "".join(failed_contents) if failed_contents else None

        # fix: qwen tool_call content field return null
        family = model_family.model_family or model_family.model_name
        if tool_calls and family in QWEN_TOOL_CALL_FAMILY and content is None:
            content = ""

        d = {
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        }

        try:
            usage = c.get("usage")
            assert "prompt_tokens" in usage
        except Exception:
            usage = {
                "prompt_tokens": -1,
                "completion_tokens": -1,
                "total_tokens": -1,
            }
        return {
            "id": "chat" + f"cmpl-{_id}",
            "model": model_uid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "choices": [
                {
                    "index": 0,
                    "delta": d,
                    "logprobs": None,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        }

    @classmethod
    def _post_process_completion(
        cls,
        model_family,
        model_uid,
        c,
        reasoning_parser: Optional[ReasoningParser] = None,
    ):
        if reasoning_parser:
            c = reasoning_parser.prepare_reasoning_content(c)
        _id = str(uuid.uuid4())
        reasoning_content = None
        if reasoning_parser and reasoning_parser.check_content_parser():
            text = c["choices"][0]["text"]
            reasoning_content, content = reasoning_parser.extract_reasoning_content(
                text
            )
            c["choices"][0]["text"] = content

        tool_result = cls._eval_tool_arguments(model_family, c)

        tool_calls = []
        failed_contents = []
        for content, func, args in tool_result:
            if func:
                tool_calls.append(
                    {
                        "id": f"call_{_id}",
                        "type": "function",
                        "function": {
                            "name": func,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                )
            else:
                if content:
                    failed_contents.append(content)
        finish_reason = "tool_calls" if tool_calls else "stop"

        content = "".join(failed_contents) if failed_contents else None

        # fix: qwen tool_call content field return null
        family = model_family.model_family or model_family.model_name
        if tool_calls and family in QWEN_TOOL_CALL_FAMILY and content is None:
            content = ""

        m = {
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        }
        # add only reasoning_content is None
        if reasoning_content is not None:
            m["reasoning_content"] = reasoning_content

        try:
            usage = c.get("usage")
            assert "prompt_tokens" in usage
        except Exception:
            usage = {
                "prompt_tokens": -1,
                "completion_tokens": -1,
                "total_tokens": -1,
            }
        return {
            "id": "chat" + f"cmpl-{_id}",
            "model": model_uid,
            "object": "chat.completion",
            "created": int(time.time()),
            "choices": [
                {
                    "index": 0,
                    "message": m,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        }

    def _transform_messages(
        self,
        messages: Union[List[ChatCompletionMessage], List[dict]],
    ):
        transformed_messages = []
        for msg in messages:
            new_content = []
            role = msg["role"]
            content = msg["content"]
            if isinstance(content, str):
                new_content.append({"type": "text", "text": content})
            elif isinstance(content, List):
                for item in content:  # type: ignore
                    if "text" in item:
                        new_content.append({"type": "text", "text": item["text"]})
                    elif "image_url" in item:
                        new_content.append(
                            {"type": "image", "image": item["image_url"]["url"]}
                        )
                    elif "video_url" in item:
                        new_content.append(
                            {"type": "video", "video": item["video_url"]["url"]}
                        )
                    elif "audio_url" in item:
                        new_content.append(
                            {"type": "audio", "audio": item["audio_url"]["url"]}
                        )
                    else:
                        logger.warning(
                            "Unknown message type, message: %s, this message may be ignored",
                            messages,
                        )
            new_message = {"role": role, "content": new_content}
            transformed_messages.append(new_message)

        return transformed_messages


def get_model_version(
    model_name: str,
    model_format: str,
    model_size_in_billions: Union[str, int],
    quantization: str,
) -> str:
    return f"{model_name}--{model_size_in_billions}B--{model_format}--{quantization}"


def _decode_image(_url):
    if _url.startswith("data:"):
        logging.info("Parse url by base64 decoder.")
        # https://platform.openai.com/docs/guides/vision/uploading-base-64-encoded-images
        # e.g. f"data:image/jpeg;base64,{base64_image}"
        _type, data = _url.split(";")
        _, ext = _type.split("/")
        data = data[len("base64,") :]
        data = base64.b64decode(data.encode("utf-8"))
        return Image.open(BytesIO(data)).convert("RGB")
    else:
        try:
            response = requests.get(_url)
        except requests.exceptions.MissingSchema:
            return Image.open(_url).convert("RGB")
        else:
            return Image.open(BytesIO(response.content)).convert("RGB")


def _decode_image_without_rgb(_url):
    if _url.startswith("data:"):
        logging.info("Parse url by base64 decoder.")
        # https://platform.openai.com/docs/guides/vision/uploading-base-64-encoded-images
        # e.g. f"data:image/jpeg;base64,{base64_image}"
        _type, data = _url.split(";")
        _, ext = _type.split("/")
        data = data[len("base64,") :]
        data = base64.b64decode(data.encode("utf-8"))
        return Image.open(BytesIO(data))
    else:
        try:
            response = requests.get(_url)
        except requests.exceptions.MissingSchema:
            return Image.open(_url)
        else:
            return Image.open(BytesIO(response.content))


@typing.no_type_check
def generate_completion_chunk(
    chunk_text: Optional[str],
    finish_reason: Optional[str],
    chunk_id: str,
    model_uid: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    has_choice: bool = True,
    has_content: bool = True,
):
    choices = []
    if has_choice:
        choices.append(
            CompletionChoice(
                text=chunk_text, index=0, logprobs=None, finish_reason=finish_reason
            )
            if has_content
            else CompletionChoice(index=0, logprobs=None, finish_reason=finish_reason)
        )
    return CompletionChunk(
        id=chunk_id,
        object="text_completion",
        created=int(time.time()),
        model=model_uid,
        choices=choices,
        usage=CompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        ),
    )


def generate_completion(
    model_uid: str,
    response: str,
    prompt_tokens=-1,
    completion_tokens=-1,
    total_tokens=-1,
    finish_reason="stop",
) -> Completion:
    return Completion(
        id=str(uuid.uuid1()),
        object="text_completion",
        created=int(time.time()),
        model=model_uid,
        choices=[
            CompletionChoice(
                text=response, index=0, logprobs=None, finish_reason=finish_reason
            )
        ],
        usage=CompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        ),
    )


def generate_chat_completion(
    model_uid: str,
    response: str,
    prompt_tokens=-1,
    completion_tokens=-1,
    total_tokens=-1,
    finish_reason="stop",
) -> ChatCompletion:
    return ChatCompletion(
        id="chat" + str(uuid.uuid1()),
        object="chat.completion",
        created=int(time.time()),
        model=model_uid,
        choices=[
            ChatCompletionChoice(
                index=0,
                message={"role": "assistant", "content": response},
                finish_reason=finish_reason,
            )
        ],
        usage=CompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        ),
    )


@functools.lru_cache
def get_stop_token_ids_from_config_file(model_path: str) -> Optional[List[int]]:
    from transformers import GenerationConfig as TransformersGenerationConfig

    transformers_config = TransformersGenerationConfig.from_pretrained(model_path)
    if transformers_config.eos_token_id is not None:
        stop_token_ids = (
            transformers_config.eos_token_id
            if isinstance(transformers_config.eos_token_id, list)
            else [transformers_config.eos_token_id]
        )
        return stop_token_ids
    return None


def parse_messages(messages: List[Dict]) -> Tuple:
    """
    Some older models still follow the old way of parameter passing.
    This function helps to parse out the needed information from OpenAI-compatible `messages`.
    """
    system_messages = [mess["content"] for mess in messages if mess["role"] == "system"]
    content_messages = [mess for mess in messages if mess["role"] != "system"]
    prompt = content_messages[-1]["content"]
    system_prompt = ". ".join(system_messages) if system_messages else None
    chat_history = content_messages[:-1]
    return prompt, system_prompt, chat_history
