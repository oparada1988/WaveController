"""Deterministic WaveController core sanity suite.

This suite deliberately excludes live PipeWire topology and daemon IPC tests.
Those checks require an active desktop audio session and are not suitable for
the default regression gate.
"""

import unittest

from tests.test_audio_invariants import (
    TestApplicationDiscoveryInvariants,
    TestHardwareDisconnectProtection,
    TestMeterMathInvariants,
    TestRoutingSubManagersInvariants,
    TestTokenMatchingInvariants,
)
from tests.test_fx_chain import TestFXChain
from tests.test_plugin_scanner import TestPluginScanner


CORE_TEST_CASES = (
    TestMeterMathInvariants,
    TestApplicationDiscoveryInvariants,
    TestHardwareDisconnectProtection,
    TestTokenMatchingInvariants,
    TestRoutingSubManagersInvariants,
    TestFXChain,
    TestPluginScanner,
)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for test_case in CORE_TEST_CASES:
        suite.addTests(loader.loadTestsFromTestCase(test_case))
    return suite