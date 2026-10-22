export default function Toasts({ items, onClose }) {
  return (
    <div className="toasts">
      {items.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`} onClick={() => onClose(t.id)}>
          {t.text}
        </div>
      ))}
    </div>
  );
}
