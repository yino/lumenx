"use client";

import { forwardRef, useRef } from "react";
import type { TextareaHTMLAttributes, UIEventHandler } from "react";
import clsx from "clsx";
import { splitAssetTagText } from "@/lib/assetTags";

interface TaggedPromptTextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
    /** Classes that mirror the textarea's font, padding, and line-height. */
    highlightClassName: string;
    /** Optional classes for the relative wrapper around the editor. */
    containerClassName?: string;
}

/**
 * Editable prompt surface with a transparent textarea over a highlighted
 * text layer. Native textareas cannot style a substring, so the overlay keeps
 * cursor, selection, keyboard shortcuts, and accessibility on the textarea
 * while rendering asset references in orange underneath it.
 */
const TaggedPromptTextarea = forwardRef<HTMLTextAreaElement, TaggedPromptTextareaProps>(
    function TaggedPromptTextarea({
        className,
        highlightClassName,
        containerClassName,
        onScroll,
        ...props
    }, forwardedRef) {
        const highlightRef = useRef<HTMLDivElement | null>(null);

        const setTextareaRef = (node: HTMLTextAreaElement | null) => {
            if (typeof forwardedRef === "function") {
                forwardedRef(node);
            } else if (forwardedRef) {
                forwardedRef.current = node;
            }
        };

        const handleScroll: UIEventHandler<HTMLTextAreaElement> = (event) => {
            const highlight = highlightRef.current;
            if (highlight) {
                highlight.scrollTop = event.currentTarget.scrollTop;
                highlight.scrollLeft = event.currentTarget.scrollLeft;
            }
            onScroll?.(event);
        };

        const value = typeof props.value === "string" ? props.value : "";
        const segments = splitAssetTagText(value);

        return (
            <div className={clsx("relative", containerClassName)}>
                <div
                    ref={highlightRef}
                    aria-hidden="true"
                    className={clsx(
                        "pointer-events-none absolute inset-0 z-0 overflow-hidden whitespace-pre-wrap break-words",
                        highlightClassName,
                    )}
                >
                    {segments.map((segment, index) => (
                        segment.isTag ? (
                            <span
                                key={`${index}-${segment.text}`}
                                className="font-semibold text-orange-500 dark:text-orange-400"
                            >
                                {segment.text}
                            </span>
                        ) : (
                            <span key={`${index}-${segment.text}`}>{segment.text}</span>
                        )
                    ))}
                </div>
                <textarea
                    {...props}
                    ref={setTextareaRef}
                    onScroll={handleScroll}
                    className={clsx(
                        "relative z-[1] text-transparent caret-foreground selection:bg-primary/25",
                        className,
                    )}
                />
            </div>
        );
    },
);

TaggedPromptTextarea.displayName = "TaggedPromptTextarea";

export default TaggedPromptTextarea;
