"use client";
import { useParams } from "next/navigation";

import { StockAlgos } from "../../../../components/StockAlgos";

export default function Page() {
  const { isin } = useParams<{ isin: string }>();
  return <StockAlgos isin={isin} />;
}
