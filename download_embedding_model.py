"""
download_embedding_model.py
============================
Download all-MiniLM-L6-v2 ONNX model for local embedding.

Run this ONCE on the machine where the plugin is installed:
    python download_embedding_model.py

This will create an `embedding_model/` folder with:
    - model.onnx        (~80MB)
    - tokenizer.json     (~700KB)

Prerequisites:
    pip install onnxruntime tokenizers huggingface_hub
    
For QGIS Python environment:
    Open OSGeo4W Shell and run:
        pip install onnxruntime tokenizers huggingface_hub --break-system-packages
"""

import os
import sys

def download_model(output_dir: str = None):
    """Download all-MiniLM-L6-v2 ONNX model and tokenizer."""
    
    if output_dir is None:
        # Default: same directory as this script
        current_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.join(current_dir, "SpatialAnalysisAgent", "embedding_model")
    
    os.makedirs(output_dir, exist_ok=True)
    
    model_path = os.path.join(output_dir, "model.onnx")
    tokenizer_path = os.path.join(output_dir, "tokenizer.json")
    
    # Check if already downloaded
    if os.path.exists(model_path) and os.path.exists(tokenizer_path):
        print(f"Model already exists at: {output_dir}")
        print(f"  model.onnx:      {os.path.getsize(model_path) / 1024 / 1024:.1f} MB")
        print(f"  tokenizer.json:  {os.path.getsize(tokenizer_path) / 1024:.0f} KB")
        return output_dir
    
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("ERROR: huggingface_hub not installed.")
        print("Run: pip install huggingface_hub")
        print()
        print("Or download manually:")
        print("  1. model.onnx from:")
        print("     https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx")
        print("  2. tokenizer.json from:")
        print("     https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/tokenizer.json")
        print(f"  3. Place both files in: {output_dir}")
        sys.exit(1)
    
    repo_id = "sentence-transformers/all-MiniLM-L6-v2"
    
    # Download model.onnx
    print("Downloading model.onnx (~80MB)...")
    downloaded_model = hf_hub_download(
        repo_id=repo_id,
        filename="onnx/model.onnx",
        local_dir=output_dir,
        local_dir_use_symlinks=False,
    )
    # Move from subfolder to output_dir root
    if not os.path.exists(model_path):
        actual_path = os.path.join(output_dir, "onnx", "model.onnx")
        if os.path.exists(actual_path):
            import shutil
            shutil.move(actual_path, model_path)
            # Clean up onnx subfolder
            onnx_dir = os.path.join(output_dir, "onnx")
            if os.path.isdir(onnx_dir) and not os.listdir(onnx_dir):
                os.rmdir(onnx_dir)
    
    print(f"  Saved: {model_path} ({os.path.getsize(model_path) / 1024 / 1024:.1f} MB)")
    
    # Download tokenizer.json
    print("Downloading tokenizer.json...")
    downloaded_tokenizer = hf_hub_download(
        repo_id=repo_id,
        filename="tokenizer.json",
        local_dir=output_dir,
        local_dir_use_symlinks=False,
    )
    print(f"  Saved: {tokenizer_path} ({os.path.getsize(tokenizer_path) / 1024:.0f} KB)")
    
    # Clean up .huggingface cache folder if created
    hf_cache = os.path.join(output_dir, ".huggingface")
    if os.path.isdir(hf_cache):
        import shutil
        shutil.rmtree(hf_cache, ignore_errors=True)
    
    print()
    print("Download complete!")
    print(f"Model directory: {output_dir}")
    print()
    print("Verify installation by running in QGIS Python Console:")
    print("  import onnxruntime; print('onnxruntime OK')")
    print("  from tokenizers import Tokenizer; print('tokenizers OK')")
    
    return output_dir


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Directory to save model files")
    args = parser.parse_args()
    download_model(args.output_dir)
