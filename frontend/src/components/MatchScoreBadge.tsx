interface MatchScoreBadgeProps {
  score: number | null;
  recommendation?: string;
  status?: string;
}

export function MatchScoreBadge({ score, recommendation, status }: MatchScoreBadgeProps) {
  const numericScore = score ?? 0;
  const showRecommendation = Boolean(recommendation && recommendation !== status && score !== null);
  const tone =
    numericScore >= 80
      ? "border-green-400/50 bg-green-400/12 text-green-200"
      : numericScore >= 60
        ? "border-cyan-300/50 bg-cyan-300/12 text-cyan-100"
        : numericScore >= 40
          ? "border-amber-300/50 bg-amber-300/12 text-amber-100"
          : "border-red-300/50 bg-red-300/12 text-red-100";

  return (
    <span className={`badge ${tone}`}>
      {score === null ? `Resume Fit: ${status || "Not Matched"}` : `Resume Fit Score: ${score}%`}
      {showRecommendation ? ` - ${recommendation}` : ""}
    </span>
  );
}
