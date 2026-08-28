import type { DataLoadStatus } from "../types/DataLoadState";

interface DataStatePanelProps {
  status: DataLoadStatus;
  error: string;
  loadingLabel: string;
  onRetry: () => void;
}

export function DataStatePanel({ status, error, loadingLabel, onRetry }: DataStatePanelProps) {
  if (status === "loading") {
    return <div className="panel p-8 text-center text-slate-400" role="status">{loadingLabel}</div>;
  }
  if (status === "error") {
    return <div className="panel p-8 text-center" role="alert"><h3 className="text-lg font-semibold text-white">Data is unavailable</h3><p className="mt-2 text-sm text-red-300">{error || "Opportunity Radar data could not be loaded."}</p><button className="btn mt-5" type="button" onClick={onRetry}>Retry</button></div>;
  }
  return null;
}
