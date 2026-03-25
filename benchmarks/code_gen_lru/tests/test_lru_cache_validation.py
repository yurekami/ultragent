"""
Validation tests for the LRU cache benchmark.
These tests verify that the generated LRU cache implementation is correct.
They are run AFTER the agent generates lru_cache.py.
"""
import importlib.util
import sys
from pathlib import Path


def load_module(module_path: str):
    """Dynamically load a Python module from path."""
    spec = importlib.util.spec_from_file_location("lru_cache", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate(work_dir: str) -> dict:
    """
    Validate the LRU cache implementation.
    Returns: {score: float, details: dict}
    """
    results = {"score": 0.0, "details": {}, "issues": []}
    work_path = Path(work_dir)

    # Check files exist
    impl_file = work_path / "lru_cache.py"
    test_file = work_path / "test_lru_cache.py"

    if not impl_file.exists():
        results["issues"].append("lru_cache.py not found")
        return results
    results["details"]["impl_exists"] = True

    if not test_file.exists():
        results["issues"].append("test_lru_cache.py not found")
        results["details"]["test_exists"] = False
    else:
        results["details"]["test_exists"] = True

    # Load and test implementation
    try:
        mod = load_module(str(impl_file))
        LRUCache = getattr(mod, "LRUCache", None)
        if LRUCache is None:
            results["issues"].append("LRUCache class not found")
            return results

        # Core functionality tests
        passed = 0
        total = 8

        # Test 1: Basic put/get
        cache = LRUCache(2)
        cache.put(1, 1)
        cache.put(2, 2)
        if cache.get(1) == 1:
            passed += 1
        else:
            results["issues"].append("Basic get failed")

        # Test 2: Capacity eviction
        cache.put(3, 3)  # Evicts key 2
        if cache.get(2) == -1:
            passed += 1
        else:
            results["issues"].append("Eviction failed: key 2 should be evicted")

        # Test 3: Recently used not evicted
        if cache.get(1) == 1:  # Key 1 was accessed, should still exist
            passed += 1
        else:
            results["issues"].append("Recently used key was evicted")

        # Test 4: Update existing key
        cache = LRUCache(2)
        cache.put(1, 1)
        cache.put(1, 10)
        if cache.get(1) == 10:
            passed += 1
        else:
            results["issues"].append("Update existing key failed")

        # Test 5: Get non-existent key
        if cache.get(999) == -1:
            passed += 1
        else:
            results["issues"].append("Get non-existent key should return -1")

        # Test 6: Capacity 1
        cache = LRUCache(1)
        cache.put(1, 1)
        cache.put(2, 2)
        if cache.get(1) == -1 and cache.get(2) == 2:
            passed += 1
        else:
            results["issues"].append("Capacity 1 handling failed")

        # Test 7: Access order matters for eviction
        cache = LRUCache(3)
        cache.put(1, 1)
        cache.put(2, 2)
        cache.put(3, 3)
        cache.get(1)       # Access 1, making 2 the LRU
        cache.put(4, 4)    # Should evict 2
        if cache.get(2) == -1 and cache.get(1) == 1:
            passed += 1
        else:
            results["issues"].append("Access order eviction failed")

        # Test 8: Large capacity
        cache = LRUCache(100)
        for i in range(100):
            cache.put(i, i * 10)
        if all(cache.get(i) == i * 10 for i in range(100)):
            passed += 1
        else:
            results["issues"].append("Large capacity test failed")

        results["details"]["validation_passed"] = passed
        results["details"]["validation_total"] = total
        results["score"] = round(passed / total, 4)

    except Exception as e:
        results["issues"].append(f"Runtime error: {e}")

    # Check for type hints
    content = impl_file.read_text(encoding="utf-8")
    has_hints = "->" in content or ": int" in content or ": str" in content
    results["details"]["has_type_hints"] = has_hints
    if has_hints:
        results["score"] = min(1.0, results["score"] + 0.1)

    return results


if __name__ == "__main__":
    work_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    result = validate(work_dir)
    import json
    print(json.dumps(result, indent=2))
