from rca_agent.adapters.secondary.embedding.bedrock_embedding import BedrockEmbeddingAdapter

_default_adapter = BedrockEmbeddingAdapter()


def embed_texts(texts, *, input_type="search_document", bedrock_client=None):
    adapter = BedrockEmbeddingAdapter(bedrock_client) if bedrock_client else _default_adapter
    return [adapter._embed_texts(texts, input_type=input_type)]  # noqa: SLF001


def embed_query(text, *, bedrock_client=None):
    adapter = BedrockEmbeddingAdapter(bedrock_client) if bedrock_client else _default_adapter
    return adapter.embed_query(text)


def embed_document(text, *, bedrock_client=None):
    adapter = BedrockEmbeddingAdapter(bedrock_client) if bedrock_client else _default_adapter
    return adapter.embed_document(text)
