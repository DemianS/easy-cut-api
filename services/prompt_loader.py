"""Загрузка версионируемых prompt-файлов из пакета API."""

from dataclasses import dataclass
import hashlib
from pathlib import Path

PROMPTS_PATH = Path(__file__).resolve().parent.parent / "prompts"


class PromptError(RuntimeError):
    """Prompt отсутствует или не содержит полезного текста."""


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    text: str
    version: str


def load_prompt(filename: str) -> PromptTemplate:
    """Прочитать prompt без возможности выйти из каталога prompts."""
    if Path(filename).name != filename:
        raise PromptError("Некорректное имя prompt-файла")
    path = PROMPTS_PATH / filename
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise PromptError("Prompt-файл недоступен") from error
    if not text:
        raise PromptError("Prompt-файл пуст")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return PromptTemplate(text=text, version=f"{path.stem}:{digest}")
