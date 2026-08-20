"use client";

import {
  forwardRef,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type ComponentPropsWithoutRef,
  type CompositionEvent,
  type KeyboardEvent,
} from "react";

interface CompositionSafeValueProps {
  value: string;
  onValueChange: (value: string) => void;
}

type ManagedEventProps =
  | "value"
  | "defaultValue"
  | "onChange"
  | "onCompositionStart"
  | "onCompositionEnd"
  | "onKeyDown"
  | "onKeyUp";

type CompositionSafeInputProps = CompositionSafeValueProps &
  Omit<ComponentPropsWithoutRef<"input">, ManagedEventProps>;

type CompositionSafeTextareaProps = CompositionSafeValueProps &
  Omit<ComponentPropsWithoutRef<"textarea">, ManagedEventProps>;

type EditableElement = HTMLInputElement | HTMLTextAreaElement;

function useCompositionSafeValue(
  value: string,
  onValueChange: (value: string) => void,
) {
  const [draft, setDraft] = useState(value);
  const isComposing = useRef(false);

  useEffect(() => {
    if (!isComposing.current) setDraft(value);
  }, [value]);

  const onChange = (event: ChangeEvent<EditableElement>) => {
    const nextValue = event.currentTarget.value;
    setDraft(nextValue);

    const nativeEvent = event.nativeEvent as InputEvent;
    if (!isComposing.current && !nativeEvent.isComposing) {
      onValueChange(nextValue);
    }
  };

  const onCompositionStart = () => {
    isComposing.current = true;
  };

  const onCompositionEnd = (event: CompositionEvent<EditableElement>) => {
    isComposing.current = false;
    const committedValue = event.currentTarget.value;
    setDraft(committedValue);
    onValueChange(committedValue);
  };

  const stopCanvasKeyboardEvent = (event: KeyboardEvent<EditableElement>) => {
    event.stopPropagation();
  };

  return {
    value: draft,
    onChange,
    onCompositionStart,
    onCompositionEnd,
    onKeyDown: stopCanvasKeyboardEvent,
    onKeyUp: stopCanvasKeyboardEvent,
  };
}

export const CompositionSafeInput = forwardRef<HTMLInputElement, CompositionSafeInputProps>(
  function CompositionSafeInput({ value, onValueChange, ...props }, ref) {
    const compositionProps = useCompositionSafeValue(value, onValueChange);
    return <input ref={ref} {...props} {...compositionProps} />;
  },
);

export const CompositionSafeTextarea = forwardRef<
  HTMLTextAreaElement,
  CompositionSafeTextareaProps
>(function CompositionSafeTextarea({ value, onValueChange, ...props }, ref) {
  const compositionProps = useCompositionSafeValue(value, onValueChange);
  return <textarea ref={ref} {...props} {...compositionProps} />;
});
