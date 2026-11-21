"use client";
import { useParams } from "next/navigation";

import { StockView } from "../../../components/StockView";

export default function Page() {
  const { isin } = useParams<{ isin: string }>();
  return <StockView isin={isin} />;
}
