// Presentation only: identities come from persisted turns; text never forms an answer.
export function questionTree(turns = [], answers = [], currentQuestion = null) {
  const answersByTurn = new Map(answers.map(answer => [answer.turn_id, answer]));
  const nodes = new Map();
  for (const turn of turns) {
    const id = turn.turn_id || turn.id;
    const text = turn.question_spoken_text || turn.question_text || turn.question?.question_text;
    if (!id || !text || nodes.has(id)) continue;
    nodes.set(id, { id, turn, text, answer: turn.answer || answersByTurn.get(id) || null, children: [] });
  }
  if (currentQuestion?.turn_id && currentQuestion.question_text) {
    const node = nodes.get(currentQuestion.turn_id);
    if (node) node.text = currentQuestion.question_text;
    else nodes.set(currentQuestion.turn_id, { id: currentQuestion.turn_id, turn: currentQuestion,
      text: currentQuestion.question_text, answer: answersByTurn.get(currentQuestion.turn_id) || null, children: [] });
  }
  const ordered = [...nodes.values()].sort((left, right) => (left.turn.order ?? Infinity) - (right.turn.order ?? Infinity));
  const roots = [];
  for (const node of ordered) {
    const parentId = node.turn.parent_turn_id || (node.turn.is_followup ? node.turn.root_turn_id : null);
    const parent = nodes.get(parentId);
    const visited = new Set([node.id]);
    let cursor = parent;
    while (cursor && !visited.has(cursor.id)) {
      visited.add(cursor.id);
      cursor = nodes.get(cursor.turn.parent_turn_id || (cursor.turn.is_followup ? cursor.turn.root_turn_id : null));
    }
    if (parent && !cursor) parent.children.push(node);
    else roots.push(node);
  }
  let rootNumber = 0;
  for (const root of roots) {
    if (root.turn.is_followup) root.label = "追问";
    else {
      const precedingFollowups = ordered.filter(node => node.turn.is_followup && node.turn.order < root.turn.order).length;
      rootNumber = Math.max(rootNumber + 1, (root.turn.order || 0) - precedingFollowups);
      root.label = `第 ${rootNumber} 题`;
    }
    let followupNumber = 0;
    const labelChildren = (node) => {
      for (const child of node.children) {
        child.label = `追问 ${++followupNumber}`;
        labelChildren(child);
      }
    };
    labelChildren(root);
  }
  return roots;
}

export function flattenQuestions(nodes) {
  return nodes.flatMap(node => [node, ...flattenQuestions(node.children)]);
}

export function questionFromAct(previous, event) {
  const { act_type: type, text } = event.payload || {};
  if (!event.turn_id || !["question", "followup", "repeat"].includes(type)) return null;
  const sameTurn = previous?.turn_id === event.turn_id;
  return {
    ...(sameTurn ? previous : {}),
    turn_id: event.turn_id,
    question_text: text,
    is_followup: type === "followup" || (sameTurn && Boolean(previous.is_followup)),
  };
}
