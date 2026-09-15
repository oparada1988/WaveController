#!/bin/bash
# WaveController Deterministic Core Sanity Hook
echo "[WaveController] Running deterministic core sanity suite..."
python3 -m unittest tests.test_core_sanity
RESULT=$?
if [ $RESULT -ne 0 ]; then
    echo ""
    echo "[WaveController] COMMIT REJECTED: Core sanity check failed."
    echo "   Live PipeWire and daemon checks are intentionally outside this gate."
    exit 1
fi
echo "[WaveController] Core sanity checks passed. Proceeding with commit."
exit 0
