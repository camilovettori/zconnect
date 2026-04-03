"use client";

import { Portal } from "@radix-ui/react-portal";
import {
  cloneElement,
  createContext,
  isValidElement,
  type ReactElement,
  type ReactNode,
  type MouseEvent as ReactMouseEvent,
  useLayoutEffect,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

type PopoverContextValue = {
  open: boolean;
  setOpen: (open: boolean) => void;
  triggerRef: React.MutableRefObject<HTMLButtonElement | null>;
  contentRef: React.MutableRefObject<HTMLDivElement | null>;
};

const PopoverContext = createContext<PopoverContextValue | null>(null);

export function usePopoverContext() {
  const context = useContext(PopoverContext);
  if (!context) {
    throw new Error("Popover components must be used within Popover");
  }
  return context;
}

export function Popover({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const onPointerDown = (event: MouseEvent | TouchEvent) => {
      const target = event.target as Node | null;
      if (!target) {
        return;
      }
      if (triggerRef.current?.contains(target) || contentRef.current?.contains(target)) {
        return;
      }
      setOpen(false);
    };

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("touchstart", onPointerDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("touchstart", onPointerDown);
    };
  }, []);

  const value = useMemo(
    () => ({ open, setOpen, triggerRef, contentRef }),
    [open],
  );

  return <PopoverContext.Provider value={value}>{children}</PopoverContext.Provider>;
}

export function PopoverTrigger({
  asChild = false,
  children,
}: {
  asChild?: boolean;
  children: ReactElement;
}) {
  const context = useContext(PopoverContext);
  if (!context) {
    throw new Error("PopoverTrigger must be used within Popover");
  }

  const { open, setOpen, triggerRef } = context;

  if (!asChild || !isValidElement(children)) {
    return (
      <button
        ref={triggerRef}
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {children}
      </button>
    );
  }

  return cloneElement(children as ReactElement<Record<string, unknown>>, {
    ref: triggerRef,
    type: "button",
    "aria-expanded": open,
    onClick: (event: ReactMouseEvent<HTMLButtonElement>) => {
      const childProps = children.props as { onClick?: (event: ReactMouseEvent<HTMLButtonElement>) => void };
      childProps.onClick?.(event);
      if (!event.defaultPrevented) {
        setOpen(!open);
      }
    },
  } as never);
}

export function PopoverContent({
  className = "",
  align = "start",
  children,
}: {
  className?: string;
  align?: "start" | "end";
  children: ReactNode;
}) {
  const context = useContext(PopoverContext);
  if (!context) {
    throw new Error("PopoverContent must be used within Popover");
  }

  const { open, contentRef } = context;
  const triggerRef = context.triggerRef;
  const [position, setPosition] = useState<{ top: number; left: number; width: number } | null>(null);

  useLayoutEffect(() => {
    if (!open || !triggerRef.current) {
      return;
    }

    const updatePosition = () => {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (!rect) {
        return;
      }

      const viewportPadding = 16;
      const preferredWidth = 340;
      const width = Math.min(preferredWidth, window.innerWidth - viewportPadding * 2);
      const left = Math.max(viewportPadding, Math.min(rect.left, window.innerWidth - width - viewportPadding));
      const top = rect.bottom + 8;
      setPosition({ top, left, width });
    };

    updatePosition();
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [open, triggerRef]);

  if (!open) {
    return null;
  }

  const alignClasses = align === "end" ? "right-0" : "left-0";

  return (
    <Portal>
      <div
        ref={contentRef}
        className={[
          "fixed z-[9999] origin-top rounded-2xl border border-slate-200 bg-white p-3 shadow-[0_24px_60px_-24px_rgba(15,23,42,0.35)] animate-[popover-in_140ms_ease-out]",
          alignClasses,
          className,
        ].join(" ")}
        style={
          position
            ? {
                top: `${position.top}px`,
                left: `${position.left}px`,
                width: `${position.width}px`,
              }
            : undefined
        }
      >
        {children}
      </div>
    </Portal>
  );
}
