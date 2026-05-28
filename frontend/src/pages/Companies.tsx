import { useMemo, useState } from "react";
import type { Company } from "../types/Company";
import type { Job } from "../types/Job";
import { CompanyCard } from "../components/CompanyCard";

interface CompaniesProps {
  companies: Company[];
  jobs: Job[];
  onViewCompanyJobs: (companyId: string) => void;
}

export function Companies({ companies, jobs, onViewCompanyJobs }: CompaniesProps) {
  const [query, setQuery] = useState("");
  const [state, setState] = useState("All");
  const [platform, setPlatform] = useState("All");
  const [feedFound, setFeedFound] = useState("All");
  const [status, setStatus] = useState("All");

  const filtered = useMemo(() => {
    return companies.filter((company) => {
      const searchable = `${company.name} ${company.state} ${company.jobPlatform} ${company.searchStatus}`.toLowerCase();
      return (
        searchable.includes(query.toLowerCase()) &&
        (state === "All" || company.state === state) &&
        (platform === "All" || company.jobPlatform === platform) &&
        (feedFound === "All" || String(company.feedFound) === feedFound) &&
        (status === "All" || company.searchStatus === status)
      );
    });
  }, [companies, feedFound, platform, query, state, status]);

  return (
    <div className="space-y-5">
      <Filters
        query={query}
        setQuery={setQuery}
        state={state}
        setState={setState}
        platform={platform}
        setPlatform={setPlatform}
        feedFound={feedFound}
        setFeedFound={setFeedFound}
        status={status}
        setStatus={setStatus}
        companies={companies}
      />
      <div className="space-y-3">
        {filtered.map((company) => (
          <CompanyCard
            key={company.id}
            company={company}
            appliedCount={jobs.filter((job) => job.companyId === company.id && job.applied).length}
            jobCount={jobs.filter((job) => job.companyId === company.id).length}
            onViewJobs={onViewCompanyJobs}
          />
        ))}
        {!filtered.length ? <Empty message="No companies match the current filters." /> : null}
      </div>
    </div>
  );
}

function Filters(props: {
  query: string;
  setQuery: (value: string) => void;
  state: string;
  setState: (value: string) => void;
  platform: string;
  setPlatform: (value: string) => void;
  feedFound: string;
  setFeedFound: (value: string) => void;
  status: string;
  setStatus: (value: string) => void;
  companies: Company[];
}) {
  const states = unique(props.companies.map((company) => company.state));
  const platforms = unique(props.companies.map((company) => company.jobPlatform));
  const statuses = unique(props.companies.map((company) => company.searchStatus));
  return (
    <div className="panel grid gap-3 p-4 md:grid-cols-2 xl:grid-cols-5">
      <input className="field" placeholder="Search name, state, platform" value={props.query} onChange={(event) => props.setQuery(event.target.value)} />
      <Select value={props.state} setValue={props.setState} values={states} label="State" />
      <Select value={props.platform} setValue={props.setPlatform} values={platforms} label="Platform" />
      <select className="field" value={props.feedFound} onChange={(event) => props.setFeedFound(event.target.value)}>
        <option>All</option>
        <option value="true">Feed found</option>
        <option value="false">Feed not found</option>
      </select>
      <Select value={props.status} setValue={props.setStatus} values={statuses} label="Status" />
    </div>
  );
}

function Select({ value, setValue, values, label }: { value: string; setValue: (value: string) => void; values: string[]; label: string }) {
  return (
    <select className="field" value={value} aria-label={label} onChange={(event) => setValue(event.target.value)}>
      <option>All</option>
      {values.map((item) => <option key={item}>{item}</option>)}
    </select>
  );
}

function unique(values: string[]) {
  return Array.from(new Set(values.filter(Boolean))).sort();
}

function Empty({ message }: { message: string }) {
  return <div className="card p-8 text-center text-slate-400">{message}</div>;
}
