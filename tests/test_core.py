import unittest

class PureLogicTests(unittest.TestCase):
    def test_case_code_range_has_9000_codes(self):
        self.assertEqual(len(range(1000, 10000)), 9000)

    def test_persian_case_code_parsing_pattern(self):
        import re
        self.assertEqual(re.search(r'(\d{4})', '#5453').group(1), '5453')

if __name__ == '__main__':
    unittest.main()
