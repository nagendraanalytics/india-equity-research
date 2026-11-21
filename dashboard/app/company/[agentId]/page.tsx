"use client";
import { useParams } from "next/navigation";

import { CompanyView } from "../../../components/CompanyView";
import { ErrorBox } from "../../../components/ui";

export default function Page() {
  const { agentId } = useParams<{ agentId: string }>();
  const id = Number(agentId);
  if (!Number.isInteger(id) || id < 0) return <ErrorBox message={`invalid agent id ${agentId}`} />;
  return <CompanyView agentId={id} />;
}
