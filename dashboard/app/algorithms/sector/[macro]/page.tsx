"use client";
import { useParams } from "next/navigation";

import { SectorAlgos } from "../../../../components/SectorAlgos";

export default function Page() {
  const { macro } = useParams<{ macro: string }>();
  return <SectorAlgos macro={decodeURIComponent(macro)} />;
}
