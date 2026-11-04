/** Starting points for each demo source. Anything else connected gets a generic one. */
export const SUGGESTIONS: Record<string, { title: string; prompt: string }[]> = {
  "demo-rental": [{
    title: "Rental performance by tenant",
    prompt: "Create a rental performance dashboard for the last 12 months covering revenue, rental agreements, the vehicle fleet and vehicle damages. I want to filter the whole dashboard by tenant name.",
  }, {
    title: "Vehicle damage and repair costs",
    prompt: "Show vehicle damage over the last 12 months: incidents, repair costs and what insurance recovered, with severity and damage type. I want to filter by tenant name.",
  }],
  "demo-shop": [{
    title: "Sales overview",
    prompt: "Show me a sales overview for the last 12 months with revenue trend, top products and revenue by region",
  }],
  "demo-marketing": [{
    title: "Marketing spend",
    prompt: "Show marketing spend for the last 12 months by channel and campaign, with clicks and conversions.",
  }],
};
