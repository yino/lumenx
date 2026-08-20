import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  CompositionSafeInput,
  CompositionSafeTextarea,
} from "@/components/canvas/CompositionSafeField";

describe("CompositionSafeField", () => {
  it("keeps intermediate pinyin local and commits the selected Chinese text", () => {
    const onValueChange = vi.fn();
    const { rerender } = render(
      <CompositionSafeTextarea
        aria-label="提示词"
        value=""
        onValueChange={onValueChange}
      />,
    );
    const field = screen.getByLabelText("提示词");

    fireEvent.compositionStart(field);
    fireEvent.change(field, { target: { value: "woxiang" } });

    expect(field).toHaveValue("woxiang");
    expect(onValueChange).not.toHaveBeenCalled();

    rerender(
      <CompositionSafeTextarea
        aria-label="提示词"
        value=""
        onValueChange={onValueChange}
      />,
    );
    expect(field).toHaveValue("woxiang");

    fireEvent.change(field, { target: { value: "我想" } });
    fireEvent.compositionEnd(field);

    expect(field).toHaveValue("我想");
    expect(onValueChange).toHaveBeenLastCalledWith("我想");
  });

  it("does not bubble Space to canvas keyboard handlers", () => {
    const canvasKeyDown = vi.fn();

    render(
      <div onKeyDown={canvasKeyDown}>
        <CompositionSafeInput
          aria-label="节点标题"
          value="新文本"
          onValueChange={vi.fn()}
        />
      </div>,
    );

    fireEvent.keyDown(screen.getByLabelText("节点标题"), {
      key: " ",
      code: "Space",
      isComposing: true,
    });

    expect(canvasKeyDown).not.toHaveBeenCalled();
  });
});
