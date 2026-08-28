#!/bin/bash
# WaveController Automated Audio Invariant Pre-Commit Hook
echo "🔍 [WaveController] Running Audio Invariant Regression Suite..."
python3 -m unittest tests/test_audio_invariants.py
RESULT=$?
if [ $RESULT -ne 0 ]; then
    echo ""
    echo "❌ [WaveController] COMMIT REJECTED: Regression invariant test failed!"
    echo "   Please fix the audio contract violation before committing."
    exit 1
fi
echo "✅ [WaveController] Audio Invariants Verified. Proceeding with commit."
exit 0
