"use client";
import { useParams } from "next/navigation";

import { FamilyDashboard } from "../../../../components/FamilyDashboard";

export default function Page() {
  const { family } = useParams<{ family: string }>();
  return <FamilyDashboard family={family} />;
}
