"""option_sentiment.py 단위 테스트 — 네트워크 없이 돈다.

스크립트에 들어 있는 자체 점검(--self-test)을 그대로 실행한다. 이론가로 만든 가상 옵션 체인에서
선도가(풋-콜 패리티), IV 역산, 스큐, 행사가 고정 변화, 가격 분해가 알려진 답과 맞는지 확인한다.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import option_sentiment  # noqa: E402


class SelfTest(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(option_sentiment.self_test(), 0)


if __name__ == "__main__":
    unittest.main()
