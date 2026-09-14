# -*- coding: utf-8 -*-
"""
Зависимости узлов графа.

Конфигурация, клиент моделей и трасса — не часть состояния: их нельзя
сериализовать вместе с прогоном и незачем гонять через рёбра. Узлы получают их
замыканием при сборке графа, а состояние остаётся чистой структурой данных,
которую видно целиком в трассе.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Deps:
    cfg: Any
    client: Any
    trace: Any
