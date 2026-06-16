from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


@dataclass
class Signal:
    symbol: str
    strategy: str
    signal_type: str  # 'buy' | 'sell' | 'hold'
    confidence: float
    stop_loss: float
    take_profit: float
    features: dict
    metadata: dict = field(default_factory=dict)


class BaseStrategy(ABC):
    def __init__(self, config: dict):
        self.config = config
        self.name = self.__class__.__name__.lower()

    @abstractmethod
    def generate_signal(self, symbol: str, df: pd.DataFrame, **kwargs) -> Optional[Signal]:
        pass

    @abstractmethod
    def should_exit(self, symbol: str, df: pd.DataFrame, trade: dict) -> bool:
        pass

    def is_enabled(self) -> bool:
        return self.config.get("enabled", True)
