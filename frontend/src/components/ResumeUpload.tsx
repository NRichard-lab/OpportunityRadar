import { useState } from "react";
import { LoaderCircle, Upload } from "lucide-react";
import { isResumeProfile, withoutResumeText, type ResumeProfile } from "../types/ResumeProfile";
import { ApiError, apiJson, userMessage } from "../api";

interface ResumeUploadProps {
  resume: ResumeProfile | null;
  onResumeChange: (resume: ResumeProfile) => Promise<void>;
}

export function ResumeUpload({ resume, onResumeChange }: ResumeUploadProps) {
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");

  const handleFile = async (file: File | null) => {
    if (!file) return;
    setUploading(true);
    setError("");
    try {
      const profile = await apiJson<unknown>(`/resume/upload?filename=${encodeURIComponent(file.name)}`, {
        method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file,
      }, "Resume upload failed.");
      if (!isResumeProfile(profile)) {
        throw new ApiError("Resume upload failed. The server returned an invalid response.");
      }
      await onResumeChange(withoutResumeText(profile));
    } catch (uploadError) {
      setError(userMessage(uploadError, "Resume upload failed."));
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="card p-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-sm font-semibold text-white">Resume upload</p>
          <p className="mt-1 text-sm text-slate-400">
            PDF and DOCX are accepted. Opportunity Radar reads the document and stores the active resume securely in its local database.
          </p>
        </div>
        <label className={`btn btn-primary ${uploading ? "cursor-not-allowed opacity-55" : "cursor-pointer"}`}>
          {uploading ? <LoaderCircle className="animate-spin" size={16} /> : <Upload size={16} />}
          {uploading ? "Reading..." : "Upload"}
          <input
            className="hidden"
            disabled={uploading}
            type="file"
            accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            onChange={(event) => handleFile(event.target.files?.[0] ?? null)}
          />
        </label>
      </div>
      {resume ? (
        <div className="mt-4 rounded-md border border-radar-line bg-radar-bg p-3 text-sm text-slate-300">
          <span className="font-medium text-white">{resume.fileName}</span> uploaded{" "}
          {new Date(resume.uploadedAt).toLocaleString()}
        </div>
      ) : (
        <div className="mt-4 rounded-md border border-dashed border-radar-line p-4 text-sm text-slate-400">
          No resume stored yet.
        </div>
      )}
      {error ? <p className="mt-3 text-sm text-red-300" role="alert">{error}</p> : null}
    </div>
  );
}
