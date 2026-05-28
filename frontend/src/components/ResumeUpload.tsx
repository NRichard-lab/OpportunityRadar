import { Upload } from "lucide-react";
import type { ResumeProfile } from "../types/ResumeProfile";
import { extractKeywords, extractResumeTextFromFileName } from "../utils/resumeMatch";

interface ResumeUploadProps {
  resume: ResumeProfile | null;
  onResumeChange: (resume: ResumeProfile) => void;
}

export function ResumeUpload({ resume, onResumeChange }: ResumeUploadProps) {
  const handleFile = (file: File | null) => {
    if (!file) return;
    const rawText = extractResumeTextFromFileName(file.name);
    const profile: ResumeProfile = {
      id: crypto.randomUUID(),
      fileName: file.name,
      uploadedAt: new Date().toISOString(),
      rawText,
      skills: extractKeywords(rawText),
      titles: [],
      yearsExperienceSummary: "TODO: Parse PDF/DOCX text in a later version.",
      notes: "File metadata is stored locally. Full PDF/DOCX parsing is stubbed for version 1.",
    };
    localStorage.setItem("financial-jobs-radar-resume", JSON.stringify(profile));
    onResumeChange(profile);
  };

  return (
    <div className="card p-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-sm font-semibold text-white">Resume upload</p>
          <p className="mt-1 text-sm text-slate-400">
            PDF and DOCX are accepted. Version 1 stores the file name and a parsing placeholder locally.
          </p>
        </div>
        <label className="btn btn-primary cursor-pointer">
          <Upload size={16} />
          Upload
          <input
            className="hidden"
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
    </div>
  );
}
