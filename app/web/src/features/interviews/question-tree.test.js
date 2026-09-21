import { describe, expect, it } from "vitest";
import { questionTree, questionFromAct } from "./question-tree.js";

describe("question and independent answer presentation", () => {
  it("numbers topics independently of intervening child turns and orphan followups", () => {
    const tree = questionTree([
      { id: "root1", order: 1, question_spoken_text: "第一主题" },
      { id: "child", order: 2, is_followup: true, parent_turn_id: "root1", question_spoken_text: "追问细节" },
      { id: "orphan", order: 3, is_followup: true, parent_turn_id: "missing", question_spoken_text: "历史追问" },
      { id: "root2", order: 4, question_spoken_text: "第二主题" },
    ], [
      { id: "a1", turn_id: "root1", final_transcript: "主回答" },
      { id: "a2", turn_id: "child", final_transcript: "追问回答" },
    ]);
    expect(tree.map(node => node.label)).toEqual(["第 1 题", "追问", "第 2 题"]);
    expect(tree[0].answer.final_transcript).toBe("主回答");
    expect(tree[0].children[0]).toMatchObject({ label: "追问 1", answer: { final_transcript: "追问回答" } });
  });

  it("groups nested followups by identity and never merges their answers", () => {
    const turns = [
      { id: "child2", order: 3, is_followup: true, parent_turn_id: "child1", root_turn_id: "root", question_spoken_text: "再次追问" },
      { id: "root", order: 1, question_spoken_text: "主问题" },
      { id: "child1", order: 2, is_followup: true, parent_turn_id: "root", root_turn_id: "root", question_spoken_text: "追问" },
      { id: "future", order: 4 },
    ];
    const tree = questionTree(turns, [
      { id: "a1", turn_id: "root", final_transcript: "主回答" },
      { id: "a2", turn_id: "child1", final_transcript: "子回答" },
    ]);
    expect(tree).toHaveLength(1);
    expect(tree[0].answer.final_transcript).toBe("主回答");
    expect(tree[0].children[0]).toMatchObject({ id: "child1", label: "追问 1", answer: { final_transcript: "子回答" } });
    expect(tree[0].children[0].children[0]).toMatchObject({ id: "child2", label: "追问 2", answer: null });
  });

  it("keeps missing-parent and cyclic historical nodes visible without attaching to an unrelated root", () => {
    const nodes = questionTree([
      { turn_id: "root", order: 1, question: { question_text: "主问题" } },
      { turn_id: "orphan", order: 2, is_followup: true, parent_turn_id: "missing", question_spoken_text: "孤立追问" },
      { turn_id: "a", order: 3, is_followup: true, parent_turn_id: "b", question_spoken_text: "A" },
      { turn_id: "b", order: 4, is_followup: true, parent_turn_id: "a", question_spoken_text: "B" },
    ]);
    const flatten = (values) => values.flatMap(node => [node.id, ...flatten(node.children)]);
    expect(flatten(nodes).sort()).toEqual(["a", "b", "orphan", "root"]);
    expect(nodes.find(node => node.id === "root").children).toEqual([]);
  });

  it("retains followup identity when repeated and clears unrelated metadata on a new turn", () => {
    const previous = { turn_id: "child", order: 2, is_followup: true, question_text: "子问题", root_turn_id: "root" };
    expect(questionFromAct(previous, { turn_id: "child", payload: { act_type: "repeat", text: "重读子问题" } }))
      .toMatchObject({ turn_id: "child", is_followup: true, order: 2, root_turn_id: "root" });
    expect(questionFromAct(previous, { turn_id: "next", payload: { act_type: "question", text: "下个主问题" } }))
      .toEqual({ turn_id: "next", question_text: "下个主问题", is_followup: false });
    expect(questionFromAct(previous, { turn_id: "child", payload: { act_type: "conversation_acknowledgement", text: "我在听" } })).toBeNull();
  });
});
