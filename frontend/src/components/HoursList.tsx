import type { BusinessHours } from "../api/client";

const DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

type HoursListProps = {
  hours: Record<string, BusinessHours>;
  today?: string;
};

export function HoursList({ hours, today }: HoursListProps) {
  return (
    <ul className="hours">
      {DAY_ORDER.filter((day) => day in hours).map((day) => {
        const entry = hours[day];
        const isOpen = Boolean(entry.open && entry.close);
        const classes = ["hours__row"];
        if (day === today) classes.push("hours__row--today");
        if (!isOpen) classes.push("hours__row--closed");

        return (
          <li key={day} className={classes.join(" ")}>
            <span className="hours__day">
              {day}
              {day === today && <span className="hours__badge">today</span>}
            </span>
            <span className="hours__time">
              {isOpen ? `${entry.open} – ${entry.close}` : "Closed"}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
