# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import unittest

from text_tools import echo


class EchoRegression(unittest.TestCase):
    def test_preserves_values_and_identity(self):
        value = ["original", 1]
        self.assertIs(echo(value), value)
        self.assertEqual(echo("hello"), "hello")


if __name__ == "__main__":
    unittest.main()
