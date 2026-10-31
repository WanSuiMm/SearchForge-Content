# Retrieval source component

This folder preserves a minimal, existing four-method retrieval library from ReportForge under the public package namespace `searchforge_content.retrieval.reportforge`. The included source files are the original package initializer, corpus/chunk loader, result models, and retrieval/index implementation. The library exposes BM25, real dense retrieval with FAISS/SentenceTransformers, reciprocal-rank fusion, and optional CrossEncoder reranking.

The top-level project installs this code as part of its `src/` package. Install the optional model dependencies from the repository root with:

```powershell
python -m pip install -e ".[retrieval]"
```

Example API (use a caller-supplied local corpus and new index path):

```python
from pathlib import Path
from searchforge_content.retrieval.reportforge.retrieval import Retriever, build_index

index_dir = Path(".local/retrieval-index")
build_index(Path("documents"), index_dir, backend="faiss", embedding_model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
retriever = Retriever(index_dir, reranker="cross-encoder", reranker_model="cross-encoder/ms-marco-MiniLM-L6-v2")
hits = retriever.search("example question", method="hybrid-rerank", top_k=10)
```

The code contains an explicit deterministic `fixture` backend for software plumbing; its hashed vectors are not learned embeddings or retrieval-quality evidence. For learned dense retrieval, select `backend="faiss"`. Model weights and caches are not included and must be supplied or fetched separately by the caller.

The adjacent `searchforge_content.retrieval.r02.ce_windowing` module is the pure overflow-safe CrossEncoder helper copied from the R02 runner. It is not wired into the ReportForge `Retriever`; this package does not claim that the general library reproduces the R02 runner's query segmentation, document-level chunk aggregation, model revisions, or scores.

Attribution: the original ReportForge project metadata declares MIT for ReportForge. No standalone upstream license text file was present in the inspected source root. The SearchForge Content package has not selected one unified relicensing term; retain this component attribution and its declared upstream term.
