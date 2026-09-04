import type { ButtonHTMLAttributes } from "react";

type DeleteIconButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "aria-label" | "title"> & {
  label: string;
};

export function DeleteIconButton({ label, className = "", ...props }: DeleteIconButtonProps) {
  return (
    <button
      {...props}
      type={props.type ?? "button"}
      className={`square-icon-button danger ${className}`.trim()}
      aria-label={label}
      title={label}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5" />
      </svg>
    </button>
  );
}
