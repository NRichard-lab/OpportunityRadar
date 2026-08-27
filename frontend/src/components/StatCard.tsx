interface StatCardProps {
  label: string;
  value: string | number;
  detail?: string;
  onClick?: () => void;
}

export function StatCard({ label, value, detail, onClick }: StatCardProps) {
  const className = `card p-4 text-left transition ${
    onClick ? "w-full cursor-pointer hover:border-radar-highlight hover:bg-radar-accent/10 focus:outline-none focus:ring-2 focus:ring-radar-accent/60" : ""
  }`;

  const content = (
    <>
      <p className="text-xs uppercase tracking-wide text-slate-400">{label}</p>
      <p className="mt-2 text-2xl font-semibold text-white">{value}</p>
      {detail ? <p className="mt-1 text-sm text-slate-400">{detail}</p> : null}
    </>
  );

  if (onClick) {
    return (
      <button className={className} onClick={onClick}>
        {content}
      </button>
    );
  }

  return (
    <div className={className}>
      {content}
    </div>
  );
}
