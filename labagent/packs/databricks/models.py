"""How packs recognise the model serving endpoints they need in a Databricks workspace."""
import re

EMBED_RE = re.compile(r"gte|bge|embed", re.I)
CHAT_RE = re.compile(r"llama|gpt|claude|gemma|qwen|instruct|chat", re.I)
