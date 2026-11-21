"use client";
import { useParams } from "next/navigation";

import { AlgoExplainer } from "../../../components/AlgoExplainer";

export default function Page() {
  const { algoId } = useParams<{ algoId: string }>();
  return <AlgoExplainer algoId={algoId} />;
}
