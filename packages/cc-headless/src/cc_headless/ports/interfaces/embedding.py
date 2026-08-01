from __future__ import annotations

from abc import ABC, abstractmethod


class EmbeddingPort(ABC):
    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """검색어를 임베딩한다.

        저장과 검색은 같은 텍스트여도 입력 유형이 다르다. 한쪽 유형으로 양쪽을 처리하면
        같은 장애의 저장 벡터와 검색 벡터가 어긋나 유사도가 낮게 나온다.
        """

    @abstractmethod
    def embed_document(self, text: str) -> list[float]: ...
