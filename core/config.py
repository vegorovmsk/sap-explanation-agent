# -*- coding: utf-8 -*-
"""
Загрузка конфигурации агента.

Три файла в config/: settings.yaml (пути к стенду, лимиты, пороги),
models.yaml (роли моделей и профили), routing.yaml (намерение → маршрут).

В значениях поддерживается подстановка переменных окружения в формате
``${VAR}`` и ``${VAR:-значение по умолчанию}``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:  # .env не обязателен, но удобен
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    """Рекурсивно подставляет переменные окружения в строки конфига."""
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            return os.environ.get(m.group(1)) or (m.group(2) or "")
        return _ENV_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _read_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Не найден файл конфигурации: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return _expand(yaml.safe_load(f) or {})


class ConfigError(RuntimeError):
    """Конфигурация несогласованна: роль без модели, модель без провайдера и т. п."""


@dataclass(frozen=True)
class ModelSpec:
    """Конкретная реализация роли: провайдер, модель, лимиты, цена."""
    ref: str                 # ключ в models.yaml, например or_reason
    role: str                # M_fast / M_balanced / M_reason / M_code / embeddings
    provider: str
    model: str
    base_url: str
    api_key: str
    headers: dict = field(default_factory=dict)
    temperature: float = 0.0
    max_tokens: int = 2048
    supports_tools: bool = True
    supports_json_schema: bool = False
    kind: str = "chat"
    price_in: float = 0.0    # $ за 1M входных токенов
    price_out: float = 0.0   # $ за 1M выходных токенов

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000


def _stand_service_years(config_file: Path) -> dict:
    """Служебные годы стенда: {год: ключ конфига}.

    Здесь только ЗНАЧЕНИЯ и ключи самого стенда. Что каждый год означает, агент
    не знает и знать не должен: смысл написан комментарием в конфиге стенда и
    читается инструментом read_stand_config, а как год влияет на расчёт — видно
    в коде. Сначала здесь стоял словарь моих толкований («2099 — заказ отложен»),
    и это было то же самое зашитое знание, только этажом ниже: агент выдавал бы
    мою формулировку за факт стенда, без координаты и без возможности проверить.
    """
    try:
        with open(config_file, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return {}
    out = {}
    for key, year in (raw.get("service_years") or {}).items():
        try:
            out[int(year)] = str(key)
        except (TypeError, ValueError):
            continue
    return out


@dataclass
class StandPaths:
    """Пути к стенду. Единственное, что агент знает о системе под наблюдением."""
    root: Path
    tasks_dir: Path
    params_dir: Path
    results_dir: Path
    logs_dir: Path
    reglaments_dir: Path
    code_dirs: list[Path]
    config_file: Path
    result_pattern: str
    log_pattern: str
    default_task: str
    sheets: dict
    pseudo_lines: list[str]
    ignore_globs: list[str]
    stage_code: dict           # этап → файлы, принимающие решения на этапе
    shared_code: list[str]     # предобработка и проверки НСИ
    report_only_code: list[str]  # выгрузка и генераторы: решений не принимают
    service_years: dict        # служебный год готовности → что он означает

    def result_for(self, task_file: str) -> Path:
        return self.results_dir / self.result_pattern.format(task_stem=Path(task_file).stem)

    def log_for(self, task_file: str) -> Path:
        return self.logs_dir / self.log_pattern.format(task_stem=Path(task_file).stem)

    def task_for(self, task_file: str) -> Path:
        return self.tasks_dir / task_file

    def is_allowed(self, path: Path) -> bool:
        """Allow-list: агент читает только внутри каталога стенда."""
        try:
            path.resolve().relative_to(self.root.resolve())
            return True
        except ValueError:
            return False


class Config:
    """Единая точка доступа к настройкам, моделям и правилам роутинга."""

    def __init__(self, profile: str | None = None):
        if load_dotenv is not None:
            load_dotenv(ROOT / ".env")
        self.root = ROOT
        self.settings = _read_yaml("settings.yaml")
        self.models_cfg = _read_yaml("models.yaml")
        self.routing = _read_yaml("routing.yaml")

        self.profile = (
            profile
            or os.environ.get("SAP_AGENT_PROFILE")
            or self.models_cfg.get("default_profile")
        )
        if self.profile not in self.models_cfg.get("profiles", {}):
            known = ", ".join(self.models_cfg.get("profiles", {}))
            raise ConfigError(f"Неизвестный профиль моделей «{self.profile}». Доступны: {known}")

        self.limits = self.settings["limits"]
        self.thresholds = self.settings["thresholds"]
        self.obs = self.settings["observability"]
        self.stand = self._build_stand()

    # ------------------------------------------------------------------ стенд
    def _build_stand(self) -> StandPaths:
        s = self.settings["stand"]
        root = (self.root / s["root"]).resolve()
        if not root.exists():
            raise ConfigError(
                f"Каталог стенда не найден: {root}. Поправьте stand.root в config/settings.yaml"
            )
        return StandPaths(
            root=root,
            tasks_dir=root / s["tasks_dir"],
            params_dir=root / s["params_dir"],
            results_dir=root / s["results_dir"],
            logs_dir=root / s["logs_dir"],
            reglaments_dir=root / s["reglaments_dir"],
            code_dirs=[root / d for d in s["code_dirs"]],
            config_file=root / s["config_file"],
            result_pattern=s["result_pattern"],
            log_pattern=s["log_pattern"],
            default_task=s["default_task"],
            sheets=s["sheets"],
            pseudo_lines=s["pseudo_lines"],
            ignore_globs=s["ignore_globs"],
            stage_code={k: list(v) for k, v in (s.get("stage_code") or {}).items()},
            shared_code=list(s.get("shared_code") or []),
            report_only_code=list(s.get("report_only_code") or []),
            service_years=_stand_service_years(root / s["config_file"]),
        )

    # ------------------------------------------------------------------ модели
    def models_for(self, role: str) -> list[ModelSpec]:
        """Цепочка реализаций роли: основная и запасные.

        В профиле роль может указывать на одну модель или на список. Список —
        это отказоустойчивость, а не роскошь: прогон золотого набора 13.09
        потерял 14 кейсов из 19, потому что у OpenRouter временно не нашлось
        провайдера с поддержкой инструментов для основной модели роли
        («404 No endpoints found that support tool use»), а деваться было некуда.
        Запасная модель той же роли закрывает и это, и упирающийся в лимит
        апстрим.
        """
        mapping = self.models_cfg["profiles"][self.profile]
        if role not in mapping:
            raise ConfigError(
                f"В профиле «{self.profile}» нет роли {role}. Есть: {', '.join(mapping)}"
            )
        refs = mapping[role]
        refs = [refs] if isinstance(refs, str) else list(refs)
        if not refs:
            raise ConfigError(f"Роль {role} в профиле «{self.profile}» не указывает ни одной модели")
        return [self._spec(ref, role) for ref in refs]

    def model_for(self, role: str) -> ModelSpec:
        """Основная реализация роли в текущем профиле."""
        return self.models_for(role)[0]

    def model_by_ref(self, ref: str) -> ModelSpec:
        """Реализация по ключу из models.yaml, минуя профиль.

        Нужна замерам: они сравнивают модели между собой, а не роли, и должны
        доставать любую из перечисленных, какой бы профиль сейчас ни стоял.
        """
        return self._spec(ref, ref)

    def _spec(self, ref: str, role: str) -> ModelSpec:
        spec = self.models_cfg["models"].get(ref)
        if spec is None:
            known = ", ".join(sorted(self.models_cfg["models"]))
            raise ConfigError(
                f"Модель «{ref}» не описана в models.yaml. Есть: {known}")

        provider_name = spec["provider"]
        provider = self.models_cfg["providers"].get(provider_name)
        if provider is None:
            raise ConfigError(f"Модель «{ref}» ссылается на неизвестного провайдера «{provider_name}»")

        api_key = provider.get("api_key") or ""
        env_name = provider.get("api_key_env")
        if env_name:
            api_key = os.environ.get(env_name, "")
            if not api_key:
                raise ConfigError(
                    f"Не задан ключ {env_name} для провайдера «{provider_name}» "
                    f"(роль {role}). Скопируйте .env.example в .env и заполните."
                )
        return ModelSpec(
            ref=ref,
            role=role,
            provider=provider_name,
            model=spec["model"],
            base_url=provider["base_url"],
            api_key=api_key,
            headers={k: v for k, v in (provider.get("headers") or {}).items() if v},
            temperature=float(spec.get("temperature", 0.0)),
            max_tokens=int(spec.get("max_tokens", 2048)),
            supports_tools=bool(spec.get("supports_tools", True)),
            supports_json_schema=bool(spec.get("supports_json_schema", False)),
            kind=spec.get("kind", "chat"),
            price_in=float(spec.get("price_in", 0.0)),
            price_out=float(spec.get("price_out", 0.0)),
        )

    def roles(self) -> list[str]:
        return list(self.models_cfg["profiles"][self.profile])

    # ----------------------------------------------------------------- роутинг
    def intents(self) -> list[str]:
        return list(self.routing["intents"])

    def route_for(self, intent: str) -> dict:
        """Маршрут намерения: роль модели и обязательные источники."""
        rule = self.routing["intents"].get(intent)
        if rule is None:
            rule = self.routing["intents"]["GENERAL_LOGIC_EXPLANATION"]
            intent = "GENERAL_LOGIC_EXPLANATION"
        return {
            "intent": intent,
            "role": rule["role"],
            "companion_role": rule.get("companion_role"),
            "required_sources": list(rule.get("required_sources", [])),
            "optional_sources": list(rule.get("optional_sources", [])),
            # Сущности, без которых у намерения нет объекта. Нужны, чтобы отличить
            # настоящую неоднозначность от придуманной: если ключевая сущность в
            # вопросе есть, уточнять нечего.
            "key_entities": list(rule.get("key_entities", [])),
            "tools": [self.routing["sources"][s] for s in rule.get("required_sources", [])
                      if s in self.routing["sources"]],
        }


_cached: Config | None = None


def get_config(profile: str | None = None, reload: bool = False) -> Config:
    global _cached
    if _cached is None or reload or (profile and profile != _cached.profile):
        _cached = Config(profile)
    return _cached
