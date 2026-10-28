from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_mimo_tts_clone.core.emotion import EmotionRouter


class EmotionRouterTests(unittest.TestCase):
    def test_classifies_chinese_emotion_keywords(self):
        router = EmotionRouter()

        self.assertEqual(router.classify("太开心了，终于成功！"), "happy")
        self.assertEqual(router.classify("我真的有点难过，想哭。"), "sad")
        self.assertEqual(router.classify("这也太离谱了，我很生气。"), "angry")
        self.assertEqual(router.classify("今天是六月十三日。"), "neutral")

    def test_manual_emotion_overrides_classification_when_supported(self):
        router = EmotionRouter()

        self.assertEqual(router.resolve("我很开心", requested="sad"), "sad")
        self.assertEqual(router.resolve("我很开心", requested="unknown"), "happy")

    def test_build_context_combines_default_emotion_voice_and_command_contexts(self):
        router = EmotionRouter(
            emotion_contexts={
                "happy": "明亮轻快",
                "neutral": "自然清晰",
            }
        )

        context = router.build_context(
            base_context="像播客主持人一样",
            emotion="happy",
            voice_context="年轻、清澈",
            command_context="稍快一点",
        )

        self.assertEqual(context, "像播客主持人一样\n明亮轻快\n年轻、清澈\n稍快一点")
