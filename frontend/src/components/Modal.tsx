import { useEffect, useRef, type ReactNode } from "react";

/** Native <dialog> modal: focus trapping, Esc to close, backdrop. */
export function Modal({
  open,
  onClose,
  title,
  children,
  actions,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  actions?: ReactNode;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      className="modal"
      style={wide ? { width: "min(820px, calc(100vw - 32px))" } : undefined}
      onClose={onClose}
      onClick={(e) => e.target === ref.current && onClose()}
      aria-labelledby="modal-title"
    >
      {open && (
        <>
          <h2 id="modal-title">{title}</h2>
          {children}
          {actions && <div className="actions">{actions}</div>}
        </>
      )}
    </dialog>
  );
}
