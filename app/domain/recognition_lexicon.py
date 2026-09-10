"""Versioned vocabulary data, never answer text or recognition substitutions."""

RECOGNITION_LEXICON_VERSION = "technical_recognition.v1"

CHINESE_TECHNICAL_TERMS = frozenset({
    "流式", "流式返回", "流式输出", "流式响应", "非流式", "分块传输",
    "检索增强生成", "向量召回", "重排", "向量化", "嵌入", "上下文窗口",
    "提示词", "文档切分", "重叠长度", "知识库", "幂等", "幂等性",
    "指数退避", "预取", "消息队列", "死信队列", "限流", "熔断", "背压",
    "并发", "异步", "协程", "缓存", "事务隔离", "水平扩展",
})

# A small vocabulary family is enabled only by the frozen current question.
# These are terminology equivalents, not model answers or inferred candidate claims.
TERM_FAMILIES = (
    (("FastAPI", "RAGFlow", "StreamingResponse", "流式"),
     ("流式", "流式返回", "流式输出", "流式响应", "非流式", "stream", "streaming", "StreamingResponse")),
    (("RAGFlow", "REST", "FastAPI"), ("REST", "REST API", "Fast API")),
    (("RAGFlow", "rerank", "重排"), ("Rerank", "Reranker", "chunk", "Embedding")),
)
