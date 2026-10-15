/** On/off switch (button with role="switch"), styled like the mockup's toggle. */
export function Toggle({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={label}
      disabled={disabled}
      className={`toggle${checked ? " on" : ""}`}
      onClick={() => onChange(!checked)}
    />
  );
}
