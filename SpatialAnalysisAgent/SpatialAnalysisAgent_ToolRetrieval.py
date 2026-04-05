"""
SpatialAnalysisAgent_ToolRetrieval.py
=====================================
Embedding-based tool retrieval for QGIS Processing tools.

Architecture:
    1. ONNX Runtime + all-MiniLM-L6-v2  (primary, ~80MB model)
    2. TF-IDF fallback                    (zero dependency)

Usage in AgentController:
    from SpatialAnalysisAgent_ToolRetrieval import ToolRetriever
    retriever = ToolRetriever(tools_doc_dir, tools_json_path)
    candidates = retriever.retrieve(query_text, top_k=20)
"""

import os
import sys
import json
import math
import pickle
import re
from collections import Counter
from typing import List, Dict, Tuple, Optional

# ---------------------------------------------------------------------------
# Try to import ONNX Runtime + tokenizers; fall back gracefully
# ---------------------------------------------------------------------------
_USE_ONNX = False
try:
    import onnxruntime as ort
    from tokenizers import Tokenizer
    _USE_ONNX = True
except ImportError:
    pass

# numpy should be available in QGIS
try:
    import numpy as np
except ImportError:
    np = None


# ===========================================================================
#  ONNX Embedder  (Primary)
# ===========================================================================
class ONNXEmbedder:
    """Local embedding using all-MiniLM-L6-v2 ONNX model."""

    def __init__(self, model_dir: str):
        """
        Parameters
        ----------
        model_dir : str
            Directory containing:
              - model.onnx          (the ONNX model file)
              - tokenizer.json      (HuggingFace tokenizer)
        """
        model_path = os.path.join(model_dir, "model.onnx")
        tokenizer_path = os.path.join(model_dir, "tokenizer.json")

        if not os.path.exists(model_path):
            raise FileNotFoundError(f"ONNX model not found: {model_path}")
        if not os.path.exists(tokenizer_path):
            raise FileNotFoundError(f"Tokenizer not found: {tokenizer_path}")

        # Load ONNX session (CPU only)
        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.intra_op_num_threads = 4
        self.session = ort.InferenceSession(model_path, sess_options,
                                            providers=["CPUExecutionProvider"])

        # Load tokenizer
        self.tokenizer = Tokenizer.from_file(tokenizer_path)
        self.tokenizer.enable_truncation(max_length=128)
        self.tokenizer.enable_padding(length=128)

        self.dim = 384  # all-MiniLM-L6-v2 output dimension

    def embed(self, texts: List[str]) -> "np.ndarray":
        """Embed a list of texts, return (N, 384) numpy array."""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)

        # Tokenize
        encoded = self.tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids, dtype=np.int64)

        # Run inference
        outputs = self.session.run(
            None,
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            },
        )

        # Mean pooling over token embeddings
        token_embeddings = outputs[0]  # (batch, seq_len, hidden_dim)
        mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
        sum_embeddings = (token_embeddings * mask_expanded).sum(axis=1)
        sum_mask = mask_expanded.sum(axis=1).clip(min=1e-9)
        sentence_embeddings = sum_embeddings / sum_mask

        # L2 normalize
        norms = np.linalg.norm(sentence_embeddings, axis=1, keepdims=True).clip(min=1e-9)
        sentence_embeddings = sentence_embeddings / norms

        return sentence_embeddings.astype(np.float32)

    def embed_single(self, text: str) -> "np.ndarray":
        """Embed a single text, return (384,) numpy array."""
        return self.embed([text])[0]


# ===========================================================================
#  TF-IDF Embedder  (Fallback — zero dependency)
# ===========================================================================
class TFIDFEmbedder:
    """Simple TF-IDF based text similarity. No external dependencies."""

    def __init__(self):
        self.vocabulary = {}   # word -> index
        self.idf = {}          # word -> idf value
        self.doc_count = 0
        self._fitted = False

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Simple whitespace + punctuation tokenizer."""
        text = text.lower()
        # Keep alphanumeric and underscores, split on everything else
        tokens = re.findall(r'[a-z0-9_]+', text)
        return tokens

    def fit(self, documents: List[str]):
        """Build vocabulary and IDF from documents."""
        self.doc_count = len(documents)
        df = Counter()  # document frequency

        for doc in documents:
            unique_tokens = set(self._tokenize(doc))
            for token in unique_tokens:
                df[token] += 1

        # Build vocabulary and IDF
        self.vocabulary = {}
        self.idf = {}
        for idx, (word, freq) in enumerate(sorted(df.items())):
            self.vocabulary[word] = idx
            self.idf[word] = math.log((self.doc_count + 1) / (freq + 1)) + 1

        self._fitted = True

    def _to_vector(self, text: str) -> Dict[str, float]:
        """Convert text to sparse TF-IDF vector."""
        tokens = self._tokenize(text)
        tf = Counter(tokens)
        total = len(tokens) if tokens else 1

        vector = {}
        for word, count in tf.items():
            if word in self.idf:
                vector[word] = (count / total) * self.idf[word]
        return vector

    def similarity(self, vec1: Dict[str, float], vec2: Dict[str, float]) -> float:
        """Cosine similarity between two sparse vectors."""
        common_keys = set(vec1.keys()) & set(vec2.keys())
        if not common_keys:
            return 0.0

        dot = sum(vec1[k] * vec2[k] for k in common_keys)
        norm1 = math.sqrt(sum(v * v for v in vec1.values()))
        norm2 = math.sqrt(sum(v * v for v in vec2.values()))

        if norm1 == 0 or norm2 == 0:
            return 0.0
        return dot / (norm1 * norm2)

    def embed_documents(self, documents: List[str]) -> List[Dict[str, float]]:
        """Embed all documents as sparse vectors."""
        if not self._fitted:
            self.fit(documents)
        return [self._to_vector(doc) for doc in documents]

    def embed_query(self, query: str) -> Dict[str, float]:
        """Embed a query as sparse vector."""
        return self._to_vector(query)


# ===========================================================================
#  Tool Index — stores tool metadata + embeddings
# ===========================================================================
class ToolIndex:
    """Holds tool metadata and pre-computed embeddings."""

    def __init__(self):
        self.tools: List[Dict] = []       # [{tool_id, tool_name, description, source}, ...]
        self.embeddings = None             # numpy array (N, dim) for ONNX, or list of dicts for TF-IDF
        self.engine: str = "none"          # "onnx" or "tfidf"
        self.version: str = "1.0"

    def save(self, path: str):
        """Save index to disk."""
        data = {
            "tools": self.tools,
            "engine": self.engine,
            "version": self.version,
        }
        if self.engine == "onnx" and self.embeddings is not None:
            data["embeddings"] = self.embeddings.tolist()
        elif self.engine == "tfidf":
            data["embeddings"] = self.embeddings  # list of dicts
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print(f"[ToolRetrieval] Index saved: {len(self.tools)} tools, engine={self.engine}")

    def load(self, path: str) -> bool:
        """Load index from disk. Returns True if successful."""
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.tools = data["tools"]
            self.engine = data.get("engine", "tfidf")
            self.version = data.get("version", "1.0")
            if self.engine == "onnx" and data.get("embeddings"):
                self.embeddings = np.array(data["embeddings"], dtype=np.float32)
            else:
                self.embeddings = data.get("embeddings", [])
            print(f"[ToolRetrieval] Index loaded: {len(self.tools)} tools, engine={self.engine}")
            return True
        except Exception as e:
            print(f"[ToolRetrieval] Failed to load index: {e}")
            return False


# ===========================================================================
#  ToolRetriever — main public class
# ===========================================================================
class ToolRetriever:
    """
    Two-stage tool retrieval:
        Stage 1: Embedding search → top-K specialized tools
        Stage 2: Merge with whitelist → send to LLM for final selection

    Parameters
    ----------
    tools_doc_dir : str
        Path to Tools_Documentation directory (contains TOML files)
    tools_json_path : str, optional
        Path to qgis340_tools.json (complete QGIS tool registry)
    model_dir : str, optional
        Path to ONNX model directory (model.onnx + tokenizer.json)
    index_path : str, optional
        Path to save/load the pre-built index
    """

    def __init__(
        self,
        tools_doc_dir: str,
        tools_json_path: str = None,
        model_dir: str = None,
        index_path: str = None,
    ):
        self.tools_doc_dir = tools_doc_dir
        self.tools_json_path = tools_json_path
        self.index_path = index_path or os.path.join(tools_doc_dir, "tool_index.json")

        # Initialize embedder
        self.onnx_embedder = None
        self.tfidf_embedder = None
        self.engine = "tfidf"  # default fallback

        if model_dir and _USE_ONNX:
            try:
                self.onnx_embedder = ONNXEmbedder(model_dir)
                self.engine = "onnx"
                print("[ToolRetrieval] Using ONNX embedding (all-MiniLM-L6-v2)")
            except Exception as e:
                print(f"[ToolRetrieval] ONNX init failed: {e}, falling back to TF-IDF")
                self.tfidf_embedder = TFIDFEmbedder()
        else:
            if not _USE_ONNX and model_dir:
                print("[ToolRetrieval] onnxruntime/tokenizers not installed, using TF-IDF fallback")
            else:
                print("[ToolRetrieval] No ONNX model dir specified, using TF-IDF fallback")
            self.tfidf_embedder = TFIDFEmbedder()

        # Load or build index
        self.index = ToolIndex()
        if not self.index.load(self.index_path):
            print("[ToolRetrieval] No existing index found, building from scratch...")
            self.build_index()

    # -------------------------------------------------------------------
    #  Index Building
    # -------------------------------------------------------------------
    def build_index(self):
        """Build embedding index from TOML files + tools JSON."""
        tools = []

        # Step 1: Parse all TOML files for rich descriptions
        toml_tools = self._parse_toml_files()
        toml_ids = set()
        for t in toml_tools:
            tools.append(t)
            toml_ids.add(t["tool_id"])

        print(f"[ToolRetrieval] Parsed {len(toml_tools)} tools from TOML files")

        # Step 2: Supplement with tools.json (tools that don't have TOML)
        if self.tools_json_path and os.path.exists(self.tools_json_path):
            with open(self.tools_json_path, "r", encoding="utf-8") as f:
                all_tools = json.load(f)

            added = 0
            for t in all_tools:
                if t["id"] not in toml_ids:
                    tools.append({
                        "tool_id": t["id"],
                        "tool_name": t["name"],
                        "description": t["name"],  # Only have the display name
                        "source": "registry",
                    })
                    added += 1
            print(f"[ToolRetrieval] Added {added} tools from QGIS registry (no TOML)")

        # Step 3: Create embedding text for each tool
        embed_texts = []
        for t in tools:
            # Combine tool_name + description for richer embedding
            text = f"{t['tool_name']}. {t['description']}"
            embed_texts.append(text)

        # Step 4: Compute embeddings
        if self.engine == "onnx" and self.onnx_embedder:
            print(f"[ToolRetrieval] Computing ONNX embeddings for {len(embed_texts)} tools...")
            # Batch to avoid memory issues
            batch_size = 64
            all_embeddings = []
            for i in range(0, len(embed_texts), batch_size):
                batch = embed_texts[i:i + batch_size]
                batch_emb = self.onnx_embedder.embed(batch)
                all_embeddings.append(batch_emb)
            embeddings = np.vstack(all_embeddings)
            self.index.embeddings = embeddings
        else:
            print(f"[ToolRetrieval] Computing TF-IDF vectors for {len(embed_texts)} tools...")
            self.tfidf_embedder = TFIDFEmbedder()
            self.tfidf_embedder.fit(embed_texts)
            self.index.embeddings = self.tfidf_embedder.embed_documents(embed_texts)

        self.index.tools = tools
        self.index.engine = self.engine

        # Save index
        self.index.save(self.index_path)
        print(f"[ToolRetrieval] Index built: {len(tools)} tools total")

    def _parse_toml_files(self) -> List[Dict]:
        """Parse TOML files to extract tool metadata."""
        tools = []

        if not os.path.exists(self.tools_doc_dir):
            print(f"[ToolRetrieval] Warning: Tools dir not found: {self.tools_doc_dir}")
            return tools

        for root, dirs, files in os.walk(self.tools_doc_dir):
            for fname in files:
                if not fname.endswith(".toml"):
                    continue

                fpath = os.path.join(root, fname)
                try:
                    tool_data = self._parse_single_toml(fpath)
                    if tool_data:
                        tools.append(tool_data)
                except Exception as e:
                    print(f"[ToolRetrieval] Error parsing {fname}: {e}")

        return tools

    @staticmethod
    def _parse_single_toml(filepath: str) -> Optional[Dict]:
        """Parse a single TOML file. Uses simple string parsing to avoid tomllib dependency."""
        tool_id = None
        tool_name = None
        brief_desc = ""

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        # Extract tool_ID
        m = re.search(r'tool_ID\s*=\s*["\']([^"\']+)["\']', content)
        if m:
            tool_id = m.group(1)

        # Extract tool_name
        m = re.search(r'tool_name\s*=\s*["\']([^"\']+)["\']', content)
        if m:
            tool_name = m.group(1)

        # Extract brief_description (may be multi-line with triple quotes)
        m = re.search(r'brief_description\s*=\s*"""(.*?)"""', content, re.DOTALL)
        if m:
            brief_desc = m.group(1).strip()
        else:
            m = re.search(r'brief_description\s*=\s*["\']([^"\']*)["\']', content)
            if m:
                brief_desc = m.group(1).strip()

        if not tool_id:
            return None

        # Clean up description
        brief_desc = " ".join(brief_desc.split())  # normalize whitespace
        if len(brief_desc) > 500:
            brief_desc = brief_desc[:500]

        return {
            "tool_id": tool_id,
            "tool_name": tool_name or tool_id.split(":")[-1],
            "description": brief_desc if brief_desc else (tool_name or tool_id),
            "source": "toml",
        }

    # -------------------------------------------------------------------
    #  Retrieval
    # -------------------------------------------------------------------
    def retrieve(self, query: str, top_k: int = 20) -> List[Dict]:
        """
        Retrieve top-K most relevant tools for a query.

        Parameters
        ----------
        query : str
            The refined task description (after Query Tuning).
        top_k : int
            Number of tools to return. Default: 20.

        Returns
        -------
        List[Dict]
            Each dict has: tool_id, tool_name, description, score
        """
        if not self.index.tools:
            print("[ToolRetrieval] Warning: Empty index, returning empty results")
            return []

        if self.index.engine == "onnx" and self.onnx_embedder:
            return self._retrieve_onnx(query, top_k)
        else:
            return self._retrieve_tfidf(query, top_k)

    def _retrieve_onnx(self, query: str, top_k: int) -> List[Dict]:
        """ONNX cosine similarity search."""
        query_vec = self.onnx_embedder.embed_single(query)  # (384,)
        embeddings = self.index.embeddings                   # (N, 384)

        # Cosine similarity (vectors are already L2-normalized)
        scores = embeddings @ query_vec  # (N,)

        # Get top-K indices
        top_indices = np.argsort(scores)[::-1][:top_k]

        results = []
        for idx in top_indices:
            tool = self.index.tools[idx].copy()
            tool["score"] = float(scores[idx])
            results.append(tool)

        return results

    def _retrieve_tfidf(self, query: str, top_k: int) -> List[Dict]:
        """TF-IDF cosine similarity search."""
        if not self.tfidf_embedder or not self.tfidf_embedder._fitted:
            # Rebuild TF-IDF from stored data
            self.tfidf_embedder = TFIDFEmbedder()
            texts = [f"{t['tool_name']}. {t['description']}" for t in self.index.tools]
            self.tfidf_embedder.fit(texts)
            self.index.embeddings = self.tfidf_embedder.embed_documents(texts)

        query_vec = self.tfidf_embedder.embed_query(query)

        # Calculate similarities
        scored = []
        for idx, doc_vec in enumerate(self.index.embeddings):
            sim = self.tfidf_embedder.similarity(query_vec, doc_vec)
            scored.append((idx, sim))

        # Sort by similarity descending
        scored.sort(key=lambda x: x[1], reverse=True)

        results = []
        for idx, score in scored[:top_k]:
            tool = self.index.tools[idx].copy()
            tool["score"] = score
            results.append(tool)

        return results

    # -------------------------------------------------------------------
    #  Supplementary Retrieval (LLM requests additional tools)
    # -------------------------------------------------------------------
    def supplement_retrieve(self, description: str, existing_ids: set, top_k: int = 10) -> List[Dict]:
        """
        Second-round retrieval when LLM signals NEED_TOOL.

        Parameters
        ----------
        description : str
            LLM's description of what tool is needed.
        existing_ids : set
            Tool IDs already in the candidate list (to avoid duplicates).
        top_k : int
            Number of additional tools to retrieve.

        Returns
        -------
        List[Dict]
            Additional tool candidates not already in existing_ids.
        """
        all_results = self.retrieve(description, top_k=top_k * 2)
        filtered = [t for t in all_results if t["tool_id"] not in existing_ids]
        return filtered[:top_k]

    # -------------------------------------------------------------------
    #  Utility: Format candidates for LLM prompt
    # -------------------------------------------------------------------
    @staticmethod
    def format_for_prompt(
        whitelist_tools: List[Dict],
        retrieved_tools: List[Dict],
    ) -> str:
        """
        Format combined tool list for the ToolSelect prompt.

        Parameters
        ----------
        whitelist_tools : List[Dict]
            Tools from TOOL_WHITELIST (always available).
        retrieved_tools : List[Dict]
            Tools from embedding retrieval.

        Returns
        -------
        str
            Formatted string for injection into ToolSelect prompt.
        """
        seen_ids = set()
        lines = []

        # Whitelist tools first (marked as common tools)
        for t in whitelist_tools:
            tid = t.get("tool_id", t.get("id", ""))
            if tid not in seen_ids:
                seen_ids.add(tid)
                name = t.get("tool_name", t.get("name", tid))
                desc = t.get("description", "")
                lines.append(f"- {name} (ID: {tid}): {desc}")

        # Retrieved tools (specialized, from embedding search)
        for t in retrieved_tools:
            tid = t["tool_id"]
            if tid not in seen_ids:
                seen_ids.add(tid)
                lines.append(f"- {t['tool_name']} (ID: {tid}): {t['description']}")

        return "\n".join(lines)


# ===========================================================================
#  Convenience: get whitelist tool info from index
# ===========================================================================
def get_whitelist_tool_info(retriever: ToolRetriever, whitelist_ids: List[str]) -> List[Dict]:
    """
    Look up whitelist tool_IDs in the index to get their names and descriptions.

    Parameters
    ----------
    retriever : ToolRetriever
        The initialized retriever with built index.
    whitelist_ids : List[str]
        List of tool_IDs from Constants.TOOL_WHITELIST.

    Returns
    -------
    List[Dict]
        Tool info dicts for each whitelist tool found in the index.
    """
    # Build lookup
    id_to_tool = {t["tool_id"]: t for t in retriever.index.tools}

    results = []
    for tid in whitelist_ids:
        if tid in id_to_tool:
            results.append(id_to_tool[tid])
        else:
            # Tool not in index but still in whitelist — add minimal info
            results.append({
                "tool_id": tid,
                "tool_name": tid.split(":")[-1],
                "description": "",
                "source": "whitelist",
            })
    return results


# ===========================================================================
#  CLI: Build index from command line
# ===========================================================================
if __name__ == "__main__":
    """
    Usage:
        python SpatialAnalysisAgent_ToolRetrieval.py --build
        python SpatialAnalysisAgent_ToolRetrieval.py --search "delineate watershed from DEM"
    """
    import argparse

    current_dir = os.path.dirname(os.path.abspath(__file__))
    default_tools_dir = os.path.join(current_dir, "Tools_Documentation")
    default_json = os.path.join(current_dir, "qgis340_tools.json")
    default_model = os.path.join(current_dir, "embedding_model")

    parser = argparse.ArgumentParser(description="Tool Retrieval for SpatialAnalysisAgent")
    parser.add_argument("--build", action="store_true", help="Build/rebuild the tool index")
    parser.add_argument("--search", type=str, help="Search for tools matching a query")
    parser.add_argument("--top-k", type=int, default=20, help="Number of results")
    parser.add_argument("--tools-dir", type=str, default=default_tools_dir)
    parser.add_argument("--tools-json", type=str, default=default_json)
    parser.add_argument("--model-dir", type=str, default=default_model)
    args = parser.parse_args()

    retriever = ToolRetriever(
        tools_doc_dir=args.tools_dir,
        tools_json_path=args.tools_json,
        model_dir=args.model_dir if os.path.exists(args.model_dir) else None,
    )

    if args.build:
        retriever.build_index()

    if args.search:
        results = retriever.retrieve(args.search, top_k=args.top_k)
        print(f"\nTop {args.top_k} results for: \"{args.search}\"")
        print("-" * 70)
        for i, r in enumerate(results, 1):
            print(f"  {i:2d}. [{r['score']:.3f}] {r['tool_name']} ({r['tool_id']})")
            if r.get("description"):
                desc = r["description"][:80] + "..." if len(r["description"]) > 80 else r["description"]
                print(f"      {desc}")
