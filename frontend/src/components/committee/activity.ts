export type CommitteeActivity = {
  status: "running";
  label: string;
  detail?: string;
};

export type CommitteeActivityReporter = (activity: CommitteeActivity | null) => void;
