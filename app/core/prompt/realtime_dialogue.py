"""Versioned prompts for the realtime speech dialogue UX track."""

REALTIME_DIALOGUE_PROMPT_VERSION = "realtime_dialogue_v1"


def session_instruction(language: str = "zh-CN") -> str:
    return (
        "你是结构化面试中的语音播报层。候选人语音只用于理解语气和保持自然衔接；"
        "你不得自行评分、改变题目难度、扩展问题或作出录用判断。"
        "只有收到本轮已批准的逐字播报指令后才能输出语音。"
        "输出语言为%s。Prompt 版本：%s。"
    ) % (language, REALTIME_DIALOGUE_PROMPT_VERSION)


def approved_spoken_response_instruction(spoken_text: str) -> str:
    text = str(spoken_text or "").strip()
    if not text:
        raise ValueError("Approved spoken response cannot be empty.")
    return (
        "只逐字朗读下面的已批准追问，不得增加称呼、解释、答案、评价或新的问题；"
        "自然、简短地播报即可：\n%s"
    ) % text
