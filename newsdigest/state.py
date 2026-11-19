"""Run-wide state, in one place so every module sees the same object.

The values are set when their modules load (storage sets _CACHE, health sets
_REPORT, ...) and replaced per run by pipeline.run()."""

_CACHE = None
_REPORT = None
_LLM_STATE = None
_FINNHUB_DISABLED = None
_BART_PIPE = None
_SEC_UA_WARNED = None
_FINNHUB_SYMBOL_CACHE = None
_GDELT_STATE = None
_GDELT_LAST = None
